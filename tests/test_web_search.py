import asyncio

from twinkle.agentserver.tools.builtin import web_search


class _FakeResp:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


_DDG_HTML = """
<html><body>
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F1&rut=xx">First Result</a>
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F2">Second</a>
  <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F3">Third</a>
</body></html>
"""


def test_parses_result_links(monkeypatch) -> None:
    async def fake_post(url, data, timeout=15.0):
        return _FakeResp(_DDG_HTML)

    monkeypatch.setattr(web_search, "_http_post", fake_post)

    async def run() -> str:
        return await web_search.web_search.invoke({"query": "hello", "max_results": 5})

    out = asyncio.run(run())
    assert "First Result" in out
    assert "https://example.com/1" in out
    assert "https://example.com/2" in out
    assert "https://example.com/3" in out


def test_respects_max_results(monkeypatch) -> None:
    async def fake_post(url, data, timeout=15.0):
        return _FakeResp(_DDG_HTML)

    monkeypatch.setattr(web_search, "_http_post", fake_post)

    async def run() -> str:
        return await web_search.web_search.invoke({"query": "hello", "max_results": 2})

    out = asyncio.run(run())
    assert "example.com/1" in out
    assert "example.com/2" in out
    assert "example.com/3" not in out
