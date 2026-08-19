import asyncio

import httpx
import pytest

from twinkle.agentserver.tools.builtin import web_fetch
from twinkle.agentserver.tools.errors import ToolError


class _FakeResp:
    """Minimal httpx-shaped response for tests."""

    def __init__(self, *, text: str = "", status_code: int = 200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self
            )

    def json(self):
        return self._json if self._json is not None else {}


_HTML = (
    "<html><head><style>x{}</style></head>"
    "<body><p>hello <b>world</b></p><script>bad</script></body></html>"
)

_TAVILY_EXTRACT_JSON = {
    "results": [
        {
            "url": "https://en.wikipedia.org/wiki/Moon",
            "raw_content": "The Moon's perigee is 356400 km, 363300 on average.",
        }
    ]
}


def _install_fake_http(monkeypatch, responder):
    """Route every _http_request call through `responder(method, url, **kw)`."""
    async def fake_request(method, url, *, headers=None, params=None,
                           data=None, json=None, timeout=15.0):
        return responder(method, url, headers=headers, params=params,
                         data=data, json=json, timeout=timeout)

    monkeypatch.setattr(web_fetch, "_http_request", fake_request)


def test_direct_get_strips_tags_and_clips(monkeypatch) -> None:
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=_HTML))

    out = asyncio.run(
        web_fetch.web_fetch.invoke({"url": "http://x", "max_chars": 8000})
    )
    assert "hello" in out and "world" in out
    assert "bad" not in out  # script dropped
    assert "<" not in out  # tags stripped


def test_truncates_over_max(monkeypatch) -> None:
    long_text = "a" * 5000
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=long_text))

    out = asyncio.run(
        web_fetch.web_fetch.invoke({"url": "http://x", "max_chars": 100})
    )
    assert len(out) < 5000
    assert "[truncated]" in out


def test_403_falls_back_to_tavily_extract(monkeypatch) -> None:
    """Real scenario from issue #12: Wikipedia returns 403 to a direct GET;
    web_fetch must fall back to Tavily extract and return real page content."""
    calls = []

    def responder(method, url, *, headers=None, params=None, data=None,
                  json=None, timeout=15.0):
        calls.append((method, url))
        if method == "GET" and "wikipedia.org" in url:
            return _FakeResp(text="denied", status_code=403)
        if method == "POST" and "tavily" in url:
            return _FakeResp(json_data=_TAVILY_EXTRACT_JSON)
        return _FakeResp(status_code=500)

    _install_fake_http(monkeypatch, responder)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    out = asyncio.run(
        web_fetch.web_fetch.invoke({"url": "https://en.wikipedia.org/wiki/Moon"})
    )
    assert "356400" in out  # real content from Tavily, not the 403 body
    assert "[error]" not in out.lower()
    # both paths exercised: direct GET then Tavily extract
    assert any(m == "GET" and "wikipedia.org" in u for m, u in calls)
    assert any(m == "POST" and "tavily" in u for m, u in calls)


def test_403_no_key_returns_honest_error_with_hint(monkeypatch) -> None:
    """Without a Tavily key, an anti-bot 403 must surface a clear, actionable
    error mentioning TAVILY_API_KEY — not '(empty page)'."""
    _install_fake_http(
        monkeypatch, lambda *a, **k: _FakeResp(text="denied", status_code=403)
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(ToolError, match="TAVILY_API_KEY"):
        asyncio.run(
            web_fetch.web_fetch.invoke({"url": "https://en.wikipedia.org/wiki/Moon"})
        )


def test_tavily_failure_after_403_aggregates_errors(monkeypatch) -> None:
    """Direct GET 403 AND Tavily extract also fails → one aggregated error."""

    def responder(method, url, *, headers=None, params=None, data=None,
                  json=None, timeout=15.0):
        if method == "GET":
            return _FakeResp(text="denied", status_code=403)
        return _FakeResp(status_code=500)  # tavily broken

    _install_fake_http(monkeypatch, responder)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    with pytest.raises(ToolError, match=r"(?i)(?=.*403)(?=.*tavily)"):
        asyncio.run(
            web_fetch.web_fetch.invoke({"url": "https://en.wikipedia.org/wiki/Moon"})
        )


def test_empty_url_returns_error(monkeypatch) -> None:
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=_HTML))
    with pytest.raises(ToolError, match="empty url"):
        asyncio.run(web_fetch.web_fetch.invoke({"url": "   "}))
