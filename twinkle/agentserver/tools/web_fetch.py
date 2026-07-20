"""web_fetch — slim read-only tool: GET a URL, return stripped text.

Rewritten from jiuwenclaw/agentserver/tools/web_fetch_tools.py (713 lines):
drops Jina Reader + trafilatura, keeps http GET + charset + HTML strip +
length clip. Uses httpx + stdlib html.parser (no extra deps).
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import httpx

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html, */*;q=0.1",
    "Accept-Language": "en-US,en;q=0.9",
}
_SKIP_TAGS = {"script", "style", "noscript", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


async def _http_get(url: str, timeout: float = 15.0) -> Any:
    """Thin httpx hook — tests monkeypatch this."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await client.get(url, headers=_HEADERS)


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return p.text()


async def web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its visible text, clipped to max_chars."""
    resp = await _http_get(url)
    resp.raise_for_status()
    text = _html_to_text(resp.text)
    if len(text) > max_chars:
        text = text[:max_chars] + "...[truncated]"
    return text or "(empty page)"
