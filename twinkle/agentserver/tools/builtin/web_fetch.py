"""web_fetch —— 稳健的只读抓取:先直接 GET,遇反爬封锁时回退 Tavily extract。

直接 GET(浏览器 UA + HTML 转文本)免费且对多数页面有效,故先跑它以
省 Tavily 配额。当站点返回 401/403/429(反爬 —— 例如 Wikipedia 对
httpx 返回 403),web_fetch 回退到 Tavily Extract API,它在服务端抓取
并返回干净可读文本,绕过封锁。无 Tavily key 时,封锁会暴露一条清晰
可操作的报错(提示 TAVILY_API_KEY),而非旧版的"(空页面)"。

设计沿用 jiuwenswarm 的"直接 GET 再走代理"骨架,但把现已被 Cloudflare
封锁的 r.jina.ai 免费阅读器替换为 Tavily extract(Twinkle 已为
web_search 配置了该 key)。httpx 原生 async,单个 ``@tool``,无额外依赖。
"""
from __future__ import annotations

import os
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.errors import ToolError

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
# 触发 Tavily extract 回退的反爬封锁状态码。
_BLOCKED_STATUSES = {401, 403, 429}
_SKIP_TAGS = {"script", "style", "noscript", "head"}


class _FetchError(Exception):
    """某条抓取路径失败时抛出,以便穿透到下一条。"""


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
    """strip;缺 scheme 时补 https://。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if urlsplit(raw).scheme in ("http", "https"):
        return raw
    return "https://" + raw


def _tavily_key() -> str:
    return (os.environ.get("TAVILY_API_KEY", "") or "").strip()


def _clip(text: str, max_chars: int) -> str:
    """max_chars <= 0 时不截断。"""
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
    """薄 httpx 钩子 —— 测试 monkeypatch 它以注入预设响应。"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if method.upper() == "GET":
            return await client.get(url, headers=headers, params=params)
        return await client.post(url, headers=headers, data=data, json=json)


async def _tavily_extract(url: str) -> str:
    """POST Tavily /extract;返回 results[0].raw_content(已是干净文本)。"""
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
    """抓取一个 URL 并返回其可见文本,截断到 max_chars。

    先尝试直接 GET(免费,对多数站点有效)。当服务器返回
    401/403/429(反爬封锁 —— 例如 Wikipedia)时,回退到 Tavily Extract API,
    它在服务端抓取并绕过封锁。无 ``TAVILY_API_KEY`` 时,封锁会暴露一条
    清晰报错(提示该 key)而非静默返回空页面。设 max_chars=0 以禁用截断。
    默认 50000 覆盖一篇典型文章的信息框 + 导语(Tavily extract 开头会带
    导航等样板,故截小了可能漏掉正文 —— 例如 Wikipedia 的近日点内容
    大约在 34KB 处)。
    """
    url = _normalize_url(url)
    if not url:
        raise ToolError("empty url", kind="validation")
    try:
        max_chars = max(0, int(max_chars or 0))
    except (TypeError, ValueError):
        max_chars = 0

    errors: list[str] = []

    # 1) 直接 GET —— 免费路径,不耗配额。
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

    # 2) Tavily extract 回退 —— 在服务端绕过反爬。
    if _tavily_key():
        try:
            return _clip(await _tavily_extract(url), max_chars)
        except Exception as exc:
            errors.append(f"tavily: {exc}")
    else:
        errors.append("no TAVILY_API_KEY for fallback")

    raise ToolError(f"fetch failed: {' | '.join(errors)}", kind="failed")
