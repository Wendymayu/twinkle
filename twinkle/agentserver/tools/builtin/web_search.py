"""web_search — layered search tool: Tavily API when a key is configured,
DuckDuckGo HTML as a zero-config fallback.

Design borrows the resilient skeleton from jiuwenswarm's search tools
(challenge detection + multi-engine fallback + error aggregation + snippet
extraction) but adapts it to Twinkle's stack: httpx-native async, a single
``@tool``, no paid-provider orchestration noise. Tavily is the reliable
primary path (clean LLM-ready JSON, no anti-bot); DDG HTML is the no-key
fallback with explicit anti-bot-challenge detection and one backoff retry, so
a rate-limited challenge page never silently surfaces as "(no results)".
"""
from __future__ import annotations

import asyncio
import os
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from twinkle.agentserver.tools.decorator import tool

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# Backoff before retrying DDG once after an anti-bot challenge; tests pin to 0.
_RETRY_DELAY = 1.5
_DD_CHALLENGE_STATUSES = {202, 418, 429, 503}
_DD_CHALLENGE_MARKERS = ("anomaly.js", "challenge-form")


class _EngineError(Exception):
    """Raised by an individual search engine to trigger fallback / reporting."""


class _ResultParser(HTMLParser):
    """Collect result__a (title, href) and result__snippet entries in order."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[list[str]] = []  # [title, url, snippet]
        self._snippets: list[str] = []
        self._in_result_a = False
        self._in_snippet = False
        self._current_href: str | None = None
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrd = dict(attrs)
        classes = attrd.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self._in_result_a = True
            self._current_href = attrd.get("href")
            self._title_parts = []
        elif tag in ("a", "div") and "result__snippet" in classes:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result_a:
            title = _strip_tags("".join(self._title_parts))
            url = _resolve_ddg_url(self._current_href or "")
            if title and url:
                self.results.append([title, url, ""])
            self._in_result_a = False
            self._current_href = None
            self._title_parts = []
        elif self._in_snippet and tag in ("a", "div"):
            self._snippets.append(_strip_tags("".join(self._snippet_parts)))
            self._in_snippet = False
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_result_a:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def paired(self) -> list[list[str]]:
        """Best-effort index pairing of snippets onto results."""
        for i, _ in enumerate(self.results):
            if i < len(self._snippets):
                self.results[i][2] = self._snippets[i]
        return self.results


def _strip_tags(value: str) -> str:
    from html import unescape
    import re

    value = re.sub(r"<[^>]+>", " ", value)
    return unescape(re.sub(r"\s+", " ", value)).strip()


def _resolve_ddg_url(href: str) -> str:
    """DDG wraps real URLs as //duckduckgo.com/l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg", [href])
    return unquote(uddg[0]) if uddg else href


def _is_ddg_challenge(status_code: int, html: str) -> bool:
    if status_code in _DD_CHALLENGE_STATUSES:
        return True
    text = (html or "").lower()
    return any(marker in text for marker in _DD_CHALLENGE_MARKERS)


def _tavily_key() -> str:
    return (os.environ.get("TAVILY_API_KEY", "") or "").strip()


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Thin httpx hook — tests monkeypatch this to inject canned responses."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if method.upper() == "GET":
            return await client.get(url, headers=headers, params=params)
        return await client.post(url, headers=headers, data=data, json=json)


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    parser = _ResultParser()
    parser.feed(html or "")
    return [
        {"title": t, "url": u, "snippet": s}
        for t, u, s in parser.paired()
    ]


async def _tavily_search(query: str, max_results: int) -> list[dict[str, str]]:
    resp = await _http_request(
        "POST",
        _TAVILY_URL,
        headers={"Content-Type": "application/json"},
        json={
            "api_key": _tavily_key(),
            "query": query,
            "topic": "general",
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    rows: list[dict[str, str]] = []
    for item in (data.get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        rows.append(
            {
                "title": str(item.get("title") or url).strip(),
                "url": url,
                "snippet": str(item.get("content") or "").strip(),
            }
        )
    if not rows:
        raise _EngineError("no results returned")
    return rows


async def _ddg_search(query: str, max_results: int) -> list[dict[str, str]]:
    for attempt in range(2):
        resp = await _http_request(
            "POST",
            _DDG_HTML_URL,
            data={"q": query, "kl": "us-en"},
            headers={"User-Agent": _USER_AGENT},
            timeout=15.0,
        )
        if _is_ddg_challenge(resp.status_code, resp.text):
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY)
                continue
            raise _EngineError("anti-bot challenge page (rate-limited); retry later")
        if resp.status_code >= 400:
            raise _EngineError(f"http {resp.status_code}")
        rows = _parse_ddg_html(resp.text)[:max_results]
        if not rows:
            raise _EngineError("no parseable results returned")
        return rows
    raise _EngineError("anti-bot challenge page (rate-limited); retry later")


def _format_rows(rows: list[dict[str, str]], engine: str, query: str) -> str:
    lines = [f"Web search results ({engine}) for: {query}"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['title']}")
        lines.append(f"   URL: {row['url']}")
        if row.get("snippet"):
            lines.append(f"   Snippet: {row['snippet']}")
    return "\n".join(lines)


@tool
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web; return up to max_results ranked title/URL/snippet lines.

    Uses the Tavily API when ``TAVILY_API_KEY`` is set; otherwise falls back to
    DuckDuckGo HTML scraping. On a DuckDuckGo anti-bot challenge (status 202 /
    anomaly.js) it retries once, and surfaces a clear error rather than the
    old silent "(no results)" when every engine is unavailable.
    """
    query = (query or "").strip()
    if not query:
        return "[error] empty query"
    max_results = max(1, min(int(max_results or 5), 20))

    errors: list[str] = []
    if _tavily_key():
        try:
            return _format_rows(await _tavily_search(query, max_results), "Tavily", query)
        except Exception as exc:
            errors.append(f"tavily: {exc}")
    try:
        return _format_rows(await _ddg_search(query, max_results), "DuckDuckGo", query)
    except Exception as exc:
        errors.append(f"duckduckgo: {exc}")

    return f"[error] search engines unavailable: {' | '.join(errors)}"
