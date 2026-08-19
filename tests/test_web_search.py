import asyncio
import os

import httpx
import pytest

from twinkle.agentserver.tools.builtin import web_search
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


_DDG_HTML = """
<html><body>
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F1&rut=xx">First Result</a>
  <a class="result__snippet">First snippet text</a>
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F2">Second</a>
  <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F3">Third</a>
</body></html>
"""

_DDG_CHALLENGE_HTML = """
<html><head><script src="/anomaly.js"></script></head>
<body><form id="challenge-form"></form></body></html>
"""

_TAVILY_JSON = {
    "results": [
        {"title": "Tavily One", "url": "https://tav.example.com/1",
         "content": "Tavily snippet one", "score": 0.9},
        {"title": "Tavily Two", "url": "https://tav.example.com/2",
         "content": "Tavily snippet two", "score": 0.8},
    ]
}


def _install_fake_http(monkeypatch, responder):
    """Route every _http_request call through `responder(method, url, **kw)`."""
    async def fake_request(method, url, *, headers=None, params=None,
                           data=None, json=None, timeout=30.0):
        return responder(method, url, headers=headers, params=params,
                         data=data, json=json, timeout=timeout)

    monkeypatch.setattr(web_search, "_http_request", fake_request)
    # no real sleeps in tests
    monkeypatch.setattr(web_search, "_RETRY_DELAY", 0.0)


def test_ddg_returns_results_with_url_and_snippet(monkeypatch) -> None:
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=_DDG_HTML))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    out = asyncio.run(
        web_search.web_search.invoke({"query": "hello", "max_results": 5})
    )
    assert "First Result" in out
    assert "https://example.com/1" in out
    assert "https://example.com/2" in out
    assert "First snippet text" in out  # snippet now surfaced


def test_ddg_respects_max_results(monkeypatch) -> None:
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=_DDG_HTML))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    out = asyncio.run(
        web_search.web_search.invoke({"query": "hello", "max_results": 2})
    )
    assert "example.com/1" in out
    assert "example.com/2" in out
    assert "example.com/3" not in out


def test_no_key_ddg_challenge_returns_honest_error_not_no_results(monkeypatch) -> None:
    """Real-evidence scenario from issue #9 trace: DDG serves a 202 anti-bot
    challenge page; tool must NOT silently return '(no results)'."""
    _install_fake_http(
        monkeypatch,
        lambda *a, **k: _FakeResp(text=_DDG_CHALLENGE_HTML, status_code=202),
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(ToolError, match="search engines unavailable"):
        asyncio.run(
            web_search.web_search.invoke({"query": "anything", "max_results": 5})
        )


def test_tavily_primary_when_key_set(monkeypatch) -> None:
    calls = []

    def responder(method, url, *, headers=None, params=None, data=None,
                  json=None, timeout=30.0):
        calls.append((method, url, json))
        if "tavily" in url:
            return _FakeResp(json_data=_TAVILY_JSON)
        return _FakeResp(text=_DDG_HTML)

    _install_fake_http(monkeypatch, responder)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    out = asyncio.run(
        web_search.web_search.invoke({"query": "hello", "max_results": 5})
    )
    assert "Tavily One" in out
    assert "tav.example.com/1" in out
    # DDG must not be queried when Tavily succeeds
    assert not any("duckduckgo" in (c[1] or "") for c in calls)


def test_tavily_failure_falls_back_to_ddg(monkeypatch) -> None:
    def responder(method, url, *, headers=None, params=None, data=None,
                  json=None, timeout=30.0):
        if "tavily" in url:
            return _FakeResp(status_code=500)
        return _FakeResp(text=_DDG_HTML)

    _install_fake_http(monkeypatch, responder)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    out = asyncio.run(
        web_search.web_search.invoke({"query": "hello", "max_results": 5})
    )
    assert "First Result" in out
    assert "DuckDuckGo" in out


def test_empty_query_returns_error(monkeypatch) -> None:
    _install_fake_http(monkeypatch, lambda *a, **k: _FakeResp(text=_DDG_HTML))
    with pytest.raises(ToolError, match="empty query"):
        asyncio.run(web_search.web_search.invoke({"query": "  "}))
