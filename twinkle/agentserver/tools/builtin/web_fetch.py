"""web_fetch — resilient read-only fetch: direct GET first, Tavily extract
fallback on anti-bot blocks.

Direct GET (browser UA + HTML-to-text) is free and works for most pages, so it
runs first to conserve the Tavily quota. When a site returns 401/403/429
(anti-bot — e.g. Wikipedia 403s httpx), web_fetch falls back to the Tavily
Extract API, which fetches server-side and returns clean readable text,
bypassing the block. Without a Tavily key, a block surfaces a clear, actionable
error (hinting at TAVILY_API_KEY) instead of the old "(empty page)".

Design follows jiuwenswarm's direct-GET-then-proxy skeleton, but replaces the
now-Cloudflare-blocked r.jina.ai free reader with Tavily extract (a key Twinkle
already configures for web_search). httpx-native async, single ``@tool``, no
extra deps.
"""
from __future__ import annotations

import os
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from twinkle.agentserver.tools.decorator import tool

_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
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
# Anti-bot block statuses that trigger the Tavily extract fallback.
_BLOCKED_STATUSES = {401, 403, 429}
_SKIP_TAGS = {"script", "style", "noscript", "head"}


class _FetchError(Exception):
    """Raised when a fetch path fails, to fall through to the next."""


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


def _normalize_url(url: str) -> str:
    """Strip; prepend https:// when the scheme is missing."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if urlsplit(raw).scheme in ("http", "https"):
        return raw
    return "https://" + raw


def _tavily_key() -> str:
    return (os.environ.get("TAVILY_API_KEY", "") or "").strip()


def _clip(text: str, max_chars: int) -> str:
    """max_chars <= 0 disables clipping."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> httpx.Response:
    """Thin httpx hook — tests monkeypatch this to inject canned responses."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if method.upper() == "GET":
            return await client.get(url, headers=headers, params=params)
        return await client.post(url, headers=headers, data=data, json=json)


async def _tavily_extract(url: str) -> str:
    """POST Tavily /extract; return results[0].raw_content (already clean text)."""
    resp = await _http_request(
        "POST",
        _TAVILY_EXTRACT_URL,
        headers={"Content-Type": "application/json"},
        json={"api_key": _tavily_key(), "urls": [url]},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    results = data.get("results") or []
    if not results:
        raise _FetchError("no results")
    content = str(results[0].get("raw_content") or "").strip()
    if not content:
        raise _FetchError("empty content")
    return content


@tool
async def web_fetch(url: str, max_chars: int = 50000) -> str:
    """Fetch a URL and return its visible text, clipped to max_chars.

    Tries a direct GET first (free, works for most sites). When the server
    returns 401/403/429 (anti-bot block — e.g. Wikipedia), falls back to the
    Tavily Extract API, which fetches server-side and bypasses the block.
    Without ``TAVILY_API_KEY``, a block surfaces a clear error hinting at the
    key rather than silently returning an empty page. Set max_chars=0 to
    disable clipping. Default 50000 covers a typical article infobox + lead
    (Tavily extract includes nav boilerplate up front, so a smaller clip can
    miss the payload — e.g. Wikipedia's perigee sits ~34KB in).
    """
    url = _normalize_url(url)
    if not url:
        return "[error] empty url"
    try:
        max_chars = max(0, int(max_chars or 0))
    except (TypeError, ValueError):
        max_chars = 0

    errors: list[str] = []

    # 1) Direct GET — free path, no quota cost.
    try:
        resp = await _http_request("GET", url, headers=_HEADERS, timeout=20.0)
        if resp.status_code in _BLOCKED_STATUSES:
            errors.append(f"direct: http {resp.status_code} (anti-bot)")
        else:
            resp.raise_for_status()
            text = _html_to_text(resp.text)
            if text:
                return _clip(text, max_chars)
            errors.append("direct: empty page")
    except Exception as exc:
        errors.append(f"direct: {exc}")

    # 2) Tavily extract fallback — bypasses anti-bot server-side.
    if _tavily_key():
        try:
            return _clip(await _tavily_extract(url), max_chars)
        except Exception as exc:
            errors.append(f"tavily: {exc}")
    else:
        errors.append("no TAVILY_API_KEY for fallback")

    return f"[error] fetch failed: {' | '.join(errors)}"
