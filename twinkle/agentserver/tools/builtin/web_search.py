"""web_search — slim read-only tool: DuckDuckGo HTML search, no API key.

Rewritten from jiuwenclaw/agentserver/tools/web_search/ (11-file
multi-provider orchestrator): drops paid providers, orchestrator, quality
layer. Keeps a single free provider (DuckDuckGo HTML endpoint) + stdlib
HTML parsing. No extra deps.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class _ResultParser(HTMLParser):
    """Collect <a class="result__a" href="...">title</a> entries."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str]] = []  # (title, url)
        self._in_result_a = False
        self._current_href: str | None = None
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        attrd = dict(attrs)
        if "result__a" in attrd.get("class", "").split():
            self._in_result_a = True
            self._current_href = attrd.get("href")
            self._title_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result_a:
            title = "".join(self._title_parts).strip()
            url = _resolve_ddg_url(self._current_href or "")
            if title and url:
                self.results.append((title, url))
            self._in_result_a = False
            self._current_href = None
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_result_a:
            self._title_parts.append(data)


def _resolve_ddg_url(href: str) -> str:
    """DDG wraps real URLs as //duckduckgo.com/l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg", [href])
    return unquote(uddg[0]) if uddg else href


async def _http_post(url: str, data: dict, timeout: float = 15.0) -> Any:
    """Thin httpx hook — tests monkeypatch this."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await client.post(
            url, data=data, headers={"User-Agent": _USER_AGENT}
        )


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo; return up to max_results title+URL lines."""
    query = (query or "").strip()
    if not query:
        return "[error] empty query"
    resp = await _http_post(_DDG_HTML_URL, {"q": query, "kl": "us-en"})
    resp.raise_for_status()
    parser = _ResultParser()
    parser.feed(resp.text)
    rows = parser.results[:max_results]
    if not rows:
        return "(no results)"
    return "\n".join(f"{i+1}. {title} — {url}" for i, (title, url) in enumerate(rows))
