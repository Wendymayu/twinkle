import asyncio

from twinkle.agentserver.tools import web_fetch


class _FakeResp:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


def test_strips_tags_and_clips(monkeypatch) -> None:
    html = "<html><head><style>x{}</style></head><body><p>hello <b>world</b></p><script>bad</script></body></html>"

    async def fake_get(url, timeout=15.0):
        return _FakeResp(html)

    monkeypatch.setattr(web_fetch, "_http_get", fake_get)

    async def run() -> str:
        return await web_fetch.web_fetch("http://x", max_chars=8000)

    out = asyncio.run(run())
    assert "hello" in out and "world" in out
    assert "bad" not in out  # script dropped
    assert "<" not in out  # tags stripped


def test_truncates_over_max(monkeypatch) -> None:
    long_text = "a" * 5000

    async def fake_get(url, timeout=15.0):
        return _FakeResp(long_text)

    monkeypatch.setattr(web_fetch, "_http_get", fake_get)

    async def run() -> str:
        return await web_fetch.web_fetch("http://x", max_chars=100)

    out = asyncio.run(run())
    assert len(out) < 5000
    assert "[truncated]" in out
