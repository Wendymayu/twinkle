# Phase 1 — agent loop 最小闭环 + 短期会话记忆 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the AgentServer's echo handler with a real OpenAI-compatible agent loop (think → tool → result → re-decide) plus in-memory session history, so multi-turn conversations with read-only web tools work end-to-end through the existing two-process skeleton.

**Architecture:** All new code lives in `twinkle/agentserver/`. `server.py`'s inline echo is replaced by a dispatch to `AgentLoop.run_stream`, an async generator yielding `E2AResponse` frames. The loop owns a `SessionStore` (in-memory, keyed by `session_id`), a `ToolRegistry` (minimal, two slim web tools rewritten from jiuwenclaw), an `LLMClient` (openai SDK wrapper), and a `LongTermMemory` stub. Gateway stays byte-for-byte unchanged — `web_channel.py` already forwards `params.session_id`. Only the browser mints `session_id`.

**Tech Stack:** Python ≥3.11, websockets ≥14, pydantic ≥2.11, **openai ≥1.50**, **httpx ≥0.27** (both new deps). OpenAI `chat.completions` streaming + native function-calling `tools`. Vite/Vue 3 frontend (tiny change to `webClient.ts`).

## Global Constraints

- Python ≥3.11, no new async test plugin — all async tests use `asyncio.run()`, no `pytest-asyncio`.
- New deps only: `openai>=1.50`, `httpx>=0.27` (httpx is also a transitive dep of openai but pin explicitly).
- LLM config via env: `TWINKLE_LLM_BASE_URL`, `TWINKLE_LLM_API_KEY`, `TWINKLE_LLM_MODEL`. Never hardcode keys.
- Session history stored as **OpenAI-native `messages`** dicts (`role`/`content`/`tool_calls`/`tool_call_id`). No normalization layer.
- Tool protocol = OpenAI function-calling (`tools=[{"type":"function","function":{...}}]`). No custom JSON schema.
- Long-term memory is a **stub** returning empty / no-op — interface shape only, do not implement real recall.
- `max_steps=8` hard cap on the agent loop; over-cap → `e2a.error` final frame.
- Gateway (`gateway/`, `e2a/`, `schema/`) is **read-only** for this phase — do not modify.
- Every task ends with a green test run + a commit. TDD: failing test first.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add `openai`, `httpx` deps |
| `twinkle/config.py` | Modify | Add `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` |
| `twinkle/agentserver/session_store.py` | Create | `SessionStore`: in-memory `dict[session_id, list[msg]]` |
| `twinkle/agentserver/memory.py` | Create | `LongTermMemory` stub (`recall`→`[]`, `store`→no-op) |
| `twinkle/agentserver/tools/registry.py` | Create | `ToolRegistry`: `schemas()` / `execute(name, args)` |
| `twinkle/agentserver/tools/web_fetch.py` | Create | Slim `web_fetch(url)` → text (httpx + stdlib HTML strip) |
| `twinkle/agentserver/tools/web_search.py` | Create | Slim `web_search(query)` via DuckDuckGo HTML, stdlib parse |
| `twinkle/agentserver/tools/__init__.py` | Create | Package init + `build_default_registry()` |
| `twinkle/agentserver/llm_client.py` | Create | `LLMClient.stream` → `TextDelta` / `Finish` events |
| `twinkle/agentserver/agent_loop.py` | Create | `AgentLoop.run_stream` / `run_unary` ReAct loop |
| `twinkle/agentserver/server.py` | Modify | `make_handler(loop)` + `build_default_loop()`; echo removed |
| `twinkle/agentserver/__main__.py` | Modify | Call `main()` which builds the loop |
| `web/src/services/webClient.ts` | Modify | Mint `session_id` uuid per conversation, send in `params` |
| `tests/test_session_store.py` | Create | SessionStore unit tests |
| `tests/test_memory_stub.py` | Create | LongTermMemory stub tests |
| `tests/test_tool_registry.py` | Create | ToolRegistry unit tests |
| `tests/test_web_fetch.py` | Create | web_fetch with mocked http |
| `tests/test_web_search.py` | Create | web_search with mocked http |
| `tests/test_llm_client.py` | Create | LLMClient with fake openai stream |
| `tests/test_agent_loop.py` | Create | AgentLoop with fake LLM (tool round-trip + multi-turn) |
| `tests/test_agentserver_handler.py` | Create | Malformed envelope + dispatch smoke (replaces echo handler tests) |
| `tests/test_integration.py` | Modify | Fake-LLM E2E through both processes (replaces echo assertion) |
| `tests/test_echo.py` | Delete | Echo handler removed |

---

### Task 1: Dependencies and config

**Files:**
- Modify: `pyproject.toml`
- Modify: `twinkle/config.py`

**Interfaces:**
- Produces: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in `twinkle.config` (later tasks import these).

- [ ] **Step 1: Add deps to pyproject**

In `pyproject.toml`, change the `dependencies` block:

```toml
dependencies = [
    "websockets>=14",
    "pydantic>=2.11",
    "openai>=1.50",
    "httpx>=0.27",
]
```

- [ ] **Step 2: Install**

Run: `pip install -e ".[dev]"`
Expected: openai + httpx installed.

- [ ] **Step 3: Add LLM config vars to config.py**

Append to `twinkle/config.py` (after the GATEWAY_PORT line):

```python
# --- LLM (OpenAI-compatible) ---
# Point at any OpenAI-compatible endpoint by overriding these env vars.
LLM_BASE_URL = os.getenv("TWINKLE_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("TWINKLE_LLM_API_KEY", "")
LLM_MODEL = os.getenv("TWINKLE_LLM_MODEL", "gpt-4o-mini")
```

- [ ] **Step 4: Verify import**

Run: `python -c "from twinkle.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL; print(LLM_MODEL)"`
Expected: prints `gpt-4o-mini` (the default).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml twinkle/config.py
git commit -m "Phase 1: add openai/httpx deps and LLM config vars"
```

---

### Task 2: SessionStore

In-memory store keyed by `session_id`, holding OpenAI-native message dicts.

**Files:**
- Create: `twinkle/agentserver/session_store.py`
- Test: `tests/test_session_store.py`

**Interfaces:**
- Produces: `SessionStore` with `get_messages(session_id) -> list[dict]` and `append(session_id, message) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_session_store.py`:

```python
from twinkle.agentserver.session_store import SessionStore


def test_append_and_get_round_trip() -> None:
    store = SessionStore()
    store.append("s1", {"role": "user", "content": "hi"})
    store.append("s1", {"role": "assistant", "content": "hello"})
    msgs = store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_sessions_are_isolated() -> None:
    store = SessionStore()
    store.append("s1", {"role": "user", "content": "a"})
    store.append("s2", {"role": "user", "content": "b"})
    assert [m["content"] for m in store.get_messages("s1")] == ["a"]
    assert [m["content"] for m in store.get_messages("s2")] == ["b"]


def test_unknown_session_returns_empty() -> None:
    assert SessionStore().get_messages("never") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: FAIL with `ModuleNotFoundError: twinkle.agentserver.session_store`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/session_store.py`:

```python
"""In-memory short-term session memory.

Stores OpenAI-native messages per session_id. Phase 1 is single-user,
in-memory; the interface allows swapping in SQLite later without
touching callers (Phase 3 will add truncate/compress here).
"""
from __future__ import annotations


class SessionStore:
    def __init__(self) -> None:
        self._data: dict[str, list[dict]] = {}

    def get_messages(self, session_id: str) -> list[dict]:
        return list(self._data.get(session_id, []))

    def append(self, session_id: str, message: dict) -> None:
        self._data.setdefault(session_id, []).append(message)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/session_store.py tests/test_session_store.py
git commit -m "Phase 1: SessionStore in-memory session history"
```

---

### Task 3: LongTermMemory stub

Stub that preserves the interface shape the agent loop will call, so real long-term memory can be slotted in later without touching the loop.

**Files:**
- Create: `twinkle/agentserver/memory.py`
- Test: `tests/test_memory_stub.py`

**Interfaces:**
- Produces: `LongTermMemory` with `recall(query) -> list[str]` (returns `[]`) and `store(fact) -> None` (no-op).

- [ ] **Step 1: Write the failing test**

`tests/test_memory_stub.py`:

```python
from twinkle.agentserver.memory import LongTermMemory


def test_recall_returns_empty() -> None:
    assert LongTermMemory().recall("anything") == []


def test_store_is_noop() -> None:
    m = LongTermMemory()
    m.store("some fact")  # must not raise
    assert m.recall("some fact") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_stub.py -v`
Expected: FAIL with `ModuleNotFoundError: twinkle.agentserver.memory`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/memory.py`:

```python
"""Long-term memory — STUB (Phase 1).

Interface shape only. recall() always returns []; store() is a no-op.
Real long-term memory (recall/store over a vector/wiki store) is
explicitly deferred per roadmap. Slotting in a real implementation later
requires no change to callers.
"""
from __future__ import annotations


class LongTermMemory:
    def recall(self, query: str) -> list[str]:
        return []

    def store(self, fact: str) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_stub.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory.py tests/test_memory_stub.py
git commit -m "Phase 1: LongTermMemory stub (interface shape only)"
```

---

### Task 4: ToolRegistry (minimal)

Minimal static registry. Phase 2 will evolve this into dynamic registration + catalog; Phase 1 keeps it small.

**Files:**
- Create: `twinkle/agentserver/tools/__init__.py`
- Create: `twinkle/agentserver/tools/registry.py`
- Test: `tests/test_tool_registry.py`

**Interfaces:**
- Produces: `ToolRegistry` with `register(name, description, parameters, execute)`, `schemas() -> list[dict]` (OpenAI tool defs), `execute(name, args) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_tool_registry.py`:

```python
import asyncio
from twinkle.agentserver.tools.registry import ToolRegistry


async def echo_tool(text: str) -> str:
    return f"echo:{text}"


def test_schemas_are_openai_function_defs() -> None:
    reg = ToolRegistry()
    reg.register(
        "echo",
        "echo back text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
    )
    schemas = reg.schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "echo back text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_unknown_tool_returns_error_string() -> None:
    reg = ToolRegistry()

    async def run() -> str:
        return await reg.execute("nope", {})

    assert asyncio.run(run()) == "[error] unknown tool: nope"


def test_execute_passes_kwargs() -> None:
    reg = ToolRegistry()
    reg.register(
        "echo",
        "echo back text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
    )

    async def run() -> str:
        return await reg.execute("echo", {"text": "hi"})

    assert asyncio.run(run()) == "echo:hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/__init__.py`:

```python
"""AgentServer tools package."""
```

`twinkle/agentserver/tools/registry.py`:

```python
"""Minimal tool registry — static registration, OpenAI function-calling schema.

Phase 2 evolves this into dynamic registration + a catalog. Phase 1 keeps
just enough to expose two read-only web tools to the agent loop.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

ToolFn = Callable[..., Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: ToolFn,
    ) -> None:
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "execute": execute,
        }

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for name, t in self._tools.items()
        ]

    async def execute(self, name: str, args: dict) -> str:
        t = self._tools.get(name)
        if t is None:
            return f"[error] unknown tool: {name}"
        try:
            return await t["execute"](**args)
        except Exception as exc:  # tool failures must not crash the loop
            return f"[tool error] {type(exc).__name__}: {exc}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_registry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/__init__.py twinkle/agentserver/tools/registry.py tests/test_tool_registry.py
git commit -m "Phase 1: minimal ToolRegistry with OpenAI function schema"
```

---

### Task 5: web_fetch tool (slim)

Slim rewrite of jiuwenclaw's `web_fetch_tools.py` (713 lines) — drops Jina/trafilatura, keeps http GET + charset + HTML strip + length clip. Uses httpx + stdlib `html.parser`.

**Files:**
- Create: `twinkle/agentserver/tools/web_fetch.py`
- Test: `tests/test_web_fetch.py`

**Interfaces:**
- Consumes: `ToolRegistry.register` (from Task 4).
- Produces: `async def web_fetch(url, max_chars=8000) -> str`, and a module-level `_http_get(url)` hook (tests monkeypatch it).

- [ ] **Step 1: Write the failing test**

`tests/test_web_fetch.py`:

```python
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
    monkeypatch.setattr(web_fetch, "_http_get", lambda url: _FakeResp(html))

    async def run() -> str:
        return await web_fetch.web_fetch("http://x", max_chars=8000)

    out = asyncio.run(run())
    assert "hello" in out and "world" in out
    assert "bad" not in out  # script dropped
    assert "<" not in out  # tags stripped


def test_truncates_over_max(monkeypatch) -> None:
    long_text = "a" * 5000
    monkeypatch.setattr(web_fetch, "_http_get", lambda url: _FakeResp(long_text))

    async def run() -> str:
        return await web_fetch.web_fetch("http://x", max_chars=100)

    out = asyncio.run(run())
    assert len(out) < 5000
    assert "[truncated]" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/web_fetch.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web_fetch.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/web_fetch.py tests/test_web_fetch.py
git commit -m "Phase 1: slim web_fetch tool (httpx + stdlib HTML strip)"
```

---

### Task 6: web_search tool (slim)

Slim rewrite of jiuwenclaw's `web_search/` (11-file multi-provider orchestrator) — drops paid providers/orchestrator/quality layer, keeps a single free provider (DuckDuckGo HTML) with stdlib parsing.

**Files:**
- Create: `twinkle/agentserver/tools/web_search.py`
- Test: `tests/test_web_search.py`

**Interfaces:**
- Consumes: `ToolRegistry.register` (Task 4).
- Produces: `async def web_search(query, max_results=5) -> str`, and module-level `_http_post(url, data)` hook for tests.

- [ ] **Step 1: Write the failing test**

`tests/test_web_search.py`:

```python
import asyncio
from urllib.parse import quote

from twinkle.agentserver.tools import web_search


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
    monkeypatch.setattr(web_search, "_http_post", lambda url, data: _FakeResp(_DDG_HTML))

    async def run() -> str:
        return await web_search.web_search("hello", max_results=5)

    out = asyncio.run(run())
    assert "First Result" in out
    assert "https://example.com/1" in out
    assert "https://example.com/2" in out
    assert "https://example.com/3" in out


def test_respects_max_results(monkeypatch) -> None:
    monkeypatch.setattr(web_search, "_http_post", lambda url, data: _FakeResp(_DDG_HTML))

    async def run() -> str:
        return await web_search.web_search("hello", max_results=2)

    out = asyncio.run(run())
    assert "example.com/1" in out
    assert "example.com/2" in out
    assert "example.com/3" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_search.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/tools/web_search.py`:

```python
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
        if "result__a" in attrd.get("class", ""):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web_search.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/web_search.py tests/test_web_search.py
git commit -m "Phase 1: slim web_search tool (DuckDuckGo HTML, stdlib parse)"
```

---

### Task 7: LLMClient

Thin wrapper over the openai SDK streaming API. Accumulates streamed tool-call fragments and emits `TextDelta` / `Finish` events. Takes an injectable `client` for testing.

**Files:**
- Create: `twinkle/agentserver/llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Produces: dataclasses `TextDelta(content: str)` and `Finish(finish_reason: str, assistant_message: dict)`; `LLMClient(base_url, api_key, model, client=None)` with `async def stream(messages, tools) -> AsyncIterator[TextDelta | Finish]`.

- [ ] **Step 1: Write the failing test**

`tests/test_llm_client.py`:

```python
import asyncio

from twinkle.agentserver.llm_client import LLMClient, TextDelta, Finish


# --- fake openai streaming shapes (mirrors openai SDK chunk objects) ---
class _Func:
    def __init__(self, name=None, arguments=""):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=""):
        self.index = index
        self.id = id
        self.function = _Func(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices):
        self.choices = choices


class _FakeCompletions:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def create(self, **kwargs):
        chunks = self._scripts[self.calls]
        self.calls += 1

        async def gen():
            for c in chunks:
                yield c

        return gen()


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, scripts):
        self.chat = _FakeChat(_FakeCompletions(scripts))


def _run(coro):
    return asyncio.run(coro)


def test_text_stream_emits_deltas_then_finish() -> None:
    scripts = [
        [
            _Chunk([_Choice(_Delta(content="hel"))]),
            _Chunk([_Choice(_Delta(content="lo"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        events = [e async for e in client.stream(messages=[{"role": "user", "content": "hi"}], tools=[])]
        return events

    events = _run(run())
    assert isinstance(events[0], TextDelta) and events[0].content == "hel"
    assert isinstance(events[1], TextDelta) and events[1].content == "lo"
    assert isinstance(events[2], Finish)
    assert events[2].finish_reason == "stop"
    assert events[2].assistant_message == {"role": "assistant", "content": "hello", "tool_calls": None}


def test_tool_call_fragments_accumulated() -> None:
    scripts = [
        [
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, id="call_1", name="web_fetch", arguments="")]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='{"url":'))])]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='"http://x"}')]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        events = [e async for e in client.stream(messages=[{"role": "user", "content": "fetch"}], tools=[])]
        return events

    events = _run(run())
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.finish_reason == "tool_calls"
    tcs = finish.assistant_message["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_1"
    assert tcs[0]["function"]["name"] == "web_fetch"
    assert tcs[0]["function"]["arguments"] == '{"url":"http://x"}'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/llm_client.py`:

```python
"""LLMClient — thin wrapper over the openai SDK streaming chat completions.

Emits two event types:
  - TextDelta(content) for each streamed text fragment
  - Finish(finish_reason, assistant_message) once, at stream end

Tool-call fragments arrive split across chunks (indexed); we accumulate
them into a single assistant_message so the agent loop can append it to
the session store and feed tool results back in the next turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from openai import AsyncOpenAI


@dataclass
class TextDelta:
    content: str


@dataclass
class Finish:
    finish_reason: str
    assistant_message: dict


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AsyncIterator[TextDelta | Finish]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        stream = await self._client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        tool_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = "stop"

        async for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                yield TextDelta(delta.content)
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    idx = tc.index
                    slot = tool_acc.setdefault(
                        idx, {"id": None, "name": None, "arguments": ""}
                    )
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        content = "".join(text_parts) or None
        tool_calls = None
        if finish_reason == "tool_calls" and tool_acc:
            tool_calls = [
                {
                    "id": tool_acc[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tool_acc[i]["name"],
                        "arguments": tool_acc[i]["arguments"],
                    },
                }
                for i in sorted(tool_acc)
            ]
        yield Finish(
            finish_reason=finish_reason,
            assistant_message={
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_llm_client.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/llm_client.py tests/test_llm_client.py
git commit -m "Phase 1: LLMClient wrapping openai streaming (TextDelta/Finish)"
```

---

### Task 8: AgentLoop

The ReAct loop. Reads/writes `SessionStore`, calls `LLMClient.stream`, dispatches tool calls through `ToolRegistry`, yields `E2AResponse` frames. `max_steps=8` cap.

**Files:**
- Create: `twinkle/agentserver/agent_loop.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `LLMClient` (Task 7), `SessionStore` (Task 2), `ToolRegistry` (Task 4), `LongTermMemory` (Task 3); `E2AEnvelope` / `E2AResponse` from `twinkle.e2a.models`.
- Produces: `AgentLoop(llm, store, tools, memory)` with `async def run_stream(env) -> AsyncIterator[E2AResponse]` and `async def run_unary(env) -> E2AResponse`.

- [ ] **Step 1: Write the failing test**

`tests/test_agent_loop.py`:

```python
import asyncio
import json

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.registry import ToolRegistry
from twinkle.e2a.models import E2AEnvelope


class _ScriptedLLM:
    """Returns one canned event-list per call, in order."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _env(query, rid="r1", session_id="s1", is_stream=True):
    return E2AEnvelope(
        request_id=rid,
        session_id=session_id,
        method="chat.send",
        params={"query": query},
        is_stream=is_stream,
    )


def _reg_with_echo_tool():
    reg = ToolRegistry()

    async def echo(text: str) -> str:
        return f"tool-saw:{text}"

    reg.register(
        "echo",
        "echo",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo,
    )
    return reg


def test_plain_answer_streams_chunks_and_complete() -> None:
    store = SessionStore()
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"
    assert final.body["result"]["content"] == "hello"


def test_tool_call_round_trip_then_answer() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    llm = _ScriptedLLM([
        # turn 1: model calls echo
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2: model produces final answer
        [TextDelta("result was "), TextDelta("good"),
         Finish("stop", {"role": "assistant", "content": "result was good", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("call echo"))]
        return frames

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "good" in final.body["result"]["content"]

    # session store now holds user, assistant(tool_calls), tool, assistant(answer)
    msgs = store.get_messages("s1")
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"]
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "c1"
    assert msgs[2]["content"] == "tool-saw:hi"
    assert msgs[3]["role"] == "assistant"


def test_cross_turn_remembers_context() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    seen_messages = []

    class _CapturingLLM:
        def __init__(self, scripts):
            self._scripts = scripts
            self.calls = 0

        async def stream(self, messages, tools):
            seen_messages.append([dict(m) for m in messages])
            events = self._scripts[self.calls]
            self.calls += 1
            for ev in events:
                yield ev

    llm = _CapturingLLM([
        [Finish("stop", {"role": "assistant", "content": "ack1", "tool_calls": None})],
        [Finish("stop", {"role": "assistant", "content": "ack2", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        async for _ in loop.run_stream(_env("turn1", rid="r1", session_id="s1")):
            pass
        async for _ in loop.run_stream(_env("turn2", rid="r2", session_id="s1")):
            pass

    asyncio.run(run())
    # turn 2's messages include turn 1's user + assistant
    assert len(seen_messages[0]) == 1   # [user]
    assert len(seen_messages[1]) == 3   # [user, assistant, user]
    assert seen_messages[1][0]["content"] == "turn1"
    assert seen_messages[1][1]["content"] == "ack1"
    assert seen_messages[1][2]["content"] == "turn2"


def test_max_steps_emits_error() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    # every turn asks for a tool call -> never converges
    tool_finish = Finish("tool_calls", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "x"}'}}]})
    llm = _ScriptedLLM([ [tool_finish] for _ in range(20) ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("loop"))]
        return frames

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.error"
    assert frames[-1].status == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`twinkle/agentserver/agent_loop.py`:

```python
"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.registry import ToolRegistry
from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.agentserver.agent_loop")

MAX_STEPS = 8


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolRegistry,
        memory: LongTermMemory,
    ) -> None:
        self._llm = llm
        self._store = store
        self._tools = tools
        self._memory = memory

    async def run_stream(self, env: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        session_id = env.session_id
        query = (env.params or {}).get("query", "")
        self._store.append(session_id, {"role": "user", "content": query})
        # long-term memory stub: recall is a no-op in Phase 1; shape preserved.
        self._memory.recall(query)

        seq = 0
        full_text = ""
        for _step in range(MAX_STEPS):
            msgs = self._store.get_messages(session_id)
            async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
                if isinstance(ev, TextDelta):
                    full_text += ev.content
                    yield E2AResponse(
                        request_id=env.request_id,
                        sequence=seq,
                        is_final=False,
                        status="in_progress",
                        response_kind="e2a.chunk",
                        body={"result": {"content": ev.content}},
                    )
                    seq += 1
                elif isinstance(ev, Finish):
                    self._store.append(session_id, ev.assistant_message)
                    tcs = ev.assistant_message.get("tool_calls")
                    if ev.finish_reason == "tool_calls" and tcs:
                        for tc in tcs:
                            name = tc["function"]["name"]
                            try:
                                args = json.loads(tc["function"]["arguments"] or "{}")
                            except Exception:
                                args = {}
                            result = await self._tools.execute(name, args)
                            self._store.append(
                                session_id,
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                },
                            )
                        continue  # re-ask model with tool results
                    yield E2AResponse(
                        request_id=env.request_id,
                        sequence=seq,
                        is_final=True,
                        status="succeeded",
                        response_kind="e2a.complete",
                        body={"result": {"content": full_text}},
                    )
                    return
        # exceeded max_steps without converging
        yield E2AResponse(
            request_id=env.request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={MAX_STEPS}"},
        )

    async def run_unary(self, env: E2AEnvelope) -> E2AResponse:
        env.is_stream = False
        final = None
        async for frame in self.run_stream(env):
            final = frame
        if final is None:
            return E2AResponse(
                request_id=env.request_id,
                is_final=True,
                status="failed",
                response_kind="e2a.error",
                body={"error": "no response"},
            )
        final.is_stream = False
        return final
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/agent_loop.py tests/test_agent_loop.py
git commit -m "Phase 1: AgentLoop ReAct core with tool round-trip and max_steps"
```

---

### Task 9: server.py dispatch + default wiring

Replace the inline echo handler with a dispatch to `AgentLoop`. Expose `make_handler(loop)` (tests inject a fake loop) and `build_default_loop()` (production, config-driven). Remove `tests/test_echo.py`; add `tests/test_agentserver_handler.py`.

**Files:**
- Modify: `twinkle/agentserver/server.py`
- Modify: `twinkle/agentserver/__main__.py`
- Delete: `tests/test_echo.py`
- Create: `tests/test_agentserver_handler.py`

**Interfaces:**
- Consumes: `AgentLoop` (Task 8), `LLMClient` (Task 7), `SessionStore`, `LongTermMemory`, `ToolRegistry`, `tools.web_fetch`/`web_search`, `config.LLM_*` (Task 1).
- Produces: `twinkle.agentserver.server.make_handler(loop)`, `build_default_loop()`, `main()`.

- [ ] **Step 1: Write the failing test**

`tests/test_agentserver_handler.py`:

```python
"""Handler-level tests: malformed envelope still errors; a valid envelope
reaches the injected loop. Replaces tests/test_echo.py (echo removed)."""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.server import make_handler
from twinkle.e2a.models import E2AEnvelope, E2AResponse


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RecordingLoop:
    """Records the env it received and streams back one canned frame."""
    def __init__(self):
        self.seen = None

    async def run_stream(self, env):
        self.seen = env
        yield E2AResponse(
            request_id=env.request_id,
            sequence=0,
            is_final=True,
            status="succeeded",
            response_kind="e2a.complete",
            body={"result": {"content": "ok"}},
        )


def test_malformed_envelope_returns_error() -> None:
    port = _free_port()
    loop_obj = _RecordingLoop()

    async def run() -> None:
        server = await serve(make_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                await ws.send("not-json-at-all")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_valid_envelope_dispatches_to_loop() -> None:
    port = _free_port()
    loop_obj = _RecordingLoop()

    async def run() -> None:
        server = await serve(make_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                env = E2AEnvelope(
                    request_id="r1", session_id="s1", method="chat.send",
                    params={"query": "hi"}, is_stream=True,
                )
                await ws.send(env.model_dump_json())
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.complete"
                assert data["body"]["result"]["content"] == "ok"
            assert loop_obj.seen is not None
            assert loop_obj.seen.session_id == "s1"
            assert loop_obj.seen.params["query"] == "hi"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
```

- [ ] **Step 2: Delete the old echo test**

```bash
git rm tests/test_echo.py
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_agentserver_handler.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_handler'`.

- [ ] **Step 4: Rewrite server.py**

Replace the entire contents of `twinkle/agentserver/server.py`. (`asyncio` is imported at the top — keep it there, do not move it to the bottom.) The module no longer exports a bare `handler` symbol; callers use `make_handler(build_default_loop())`:

```python
"""AgentServer — the heavy execution core process.

Phase 1: a `websockets` server that dispatches inbound E2A envelopes to an
AgentLoop (ReAct: think -> tool -> result -> re-decide). echo is gone.

make_handler(loop) lets tests inject a fake loop; build_default_loop()
wires the real config-driven loop for production.
"""
from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools import build_default_registry
from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.agentserver")

ACK_FRAME = {
    "type": "event",
    "event": EventType.CONNECTION_ACK.value,
    "payload": {"status": "ready"},
}


def build_default_loop() -> AgentLoop:
    """Production wiring — config-driven LLM + default tool registry."""
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    store = SessionStore()
    tools = build_default_registry()
    memory = LongTermMemory()
    return AgentLoop(llm, store, tools, memory)


def make_handler(loop: AgentLoop):
    """Return a ws handler bound to the given AgentLoop."""

    async def handler(ws) -> None:
        await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
        async for raw in ws:
            try:
                env = E2AEnvelope.model_validate_json(raw)
            except Exception as exc:
                err = E2AResponse(
                    request_id="?",
                    status="failed",
                    response_kind="e2a.error",
                    body={"error": str(exc)},
                )
                await ws.send(err.model_dump_json())
                continue
            try:
                if env.is_stream:
                    async for frame in loop.run_stream(env):
                        await ws.send(frame.model_dump_json())
                else:
                    frame = await loop.run_unary(env)
                    await ws.send(frame.model_dump_json())
            except Exception as exc:
                log.exception("agent loop failed for %s: %s", env.request_id, exc)
                err = E2AResponse(
                    request_id=env.request_id,
                    status="failed",
                    response_kind="e2a.error",
                    body={"error": str(exc)},
                )
                await ws.send(err.model_dump_json())

    return handler


async def main() -> None:
    h = make_handler(build_default_loop())
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(h, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
```

- [ ] **Step 5: Add build_default_registry to tools package**

Replace `twinkle/agentserver/tools/__init__.py`:

```python
"""AgentServer tools package + default registry builder."""
from __future__ import annotations

from twinkle.agentserver.tools import web_fetch, web_search
from twinkle.agentserver.tools.registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    """Register the Phase 1 read-only tools. Phase 2 evolves this."""
    reg = ToolRegistry()
    reg.register(
        name="web_fetch",
        description="Fetch a URL and return its visible text content.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
                "max_chars": {"type": "integer", "default": 8000, "description": "Max chars to return."},
            },
            "required": ["url"],
        },
        execute=web_fetch.web_fetch,
    )
    reg.register(
        name="web_search",
        description="Search the web via DuckDuckGo; returns title + URL lines.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "default": 5, "description": "Max results to return."},
            },
            "required": ["query"],
        },
        execute=web_search.web_search,
    )
    return reg
```

- [ ] **Step 6: Verify __main__ still works**

`twinkle/agentserver/__main__.py` already calls `from twinkle.agentserver.server import main` then `asyncio.run(main())` — no change needed. Confirm by importing:

Run: `python -c "from twinkle.agentserver.server import main, make_handler, build_default_loop; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_agentserver_handler.py -v`
Expected: 2 passed.

- [ ] **Step 8: Run full suite to confirm nothing else broke**

Run: `python -m pytest tests/ -v`
Expected: all green EXCEPT `tests/test_integration.py` which now fails on `assembled == "Echo: hello"` (fixed in Task 11). If other tests reference the removed `handler` symbol, note them — Task 11 fixes integration.

- [ ] **Step 9: Commit**

```bash
git add twinkle/agentserver/server.py twinkle/agentserver/tools/__init__.py tests/test_agentserver_handler.py
git commit -m "Phase 1: server.py dispatches to AgentLoop; make_handler + build_default_loop"
```

---

### Task 10: Browser mints session_id

`web_channel.py` already reads `params.session_id` and forwards it — gateway unchanged. Only the browser needs to mint a uuid per conversation and include it in the request.

**Files:**
- Modify: `web/src/services/webClient.ts`

**Interfaces:**
- Produces: `WebClient` includes `session_id` in every `req.params`; `send` gains an optional `sessionId` getter; a conversation id is minted on `connect`.

- [ ] **Step 1: Edit webClient.ts**

In `web/src/services/webClient.ts`, add a `sessionId` field minted on connect, and include it in the sent frame. Replace the class body's connect/send portions:

Change the class fields (after `private seq = 0`):

```typescript
  private seq = 0
  private sessionId = ''
```

Replace the `connect` method's `onopen`:

```typescript
  connect(onReady: () => void): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws.onopen = () => {
      // mint a fresh conversation id per connection (browser-driven session,
      // matches roadmap Phase 1: gateway stays a dumb relay)
      this.sessionId = 'sess_' + crypto.randomUUID()
      onReady()
    }
    this.ws.onmessage = (ev) => {
      try {
        this.handle(JSON.parse(ev.data))
      } catch (e) {
        console.error('bad frame', e)
      }
    }
  }
```

Replace the `send` method to include `session_id` in params:

```typescript
  send(method: string, params: Record<string, any>): string {
    const id = 'req_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    const fullParams = { ...params, session_id: this.sessionId }
    this.ws?.send(JSON.stringify({ type: 'req', id, method, params: fullParams }))
    return id
  }
```

- [ ] **Step 2: Verify it typechecks**

Run: `cd web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add web/src/services/webClient.ts
git commit -m "Phase 1: browser mints session_id and sends it in params"
```

---

### Task 11: E2E integration test (fake LLM through both processes)

Replace the Phase 0 echo E2E assertion with a Phase 1 E2E that runs a real `AgentLoop` with a fake `LLMClient` through both processes, asserting multi-turn context + a tool round-trip across the wire. This is the roadmap M2 acceptance, exercised headlessly and deterministically (no real API key needed).

**Files:**
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: `make_handler` + `AgentLoop` + `SessionStore` + `ToolRegistry` (Tasks 4, 8, 9).

- [ ] **Step 1: Rewrite the integration test**

Replace the entire contents of `tests/test_integration.py`:

```python
"""End-to-end Phase 1 integration: the full browser -> gateway ->
agentserver -> gateway -> browser round trip, driven by a REAL AgentLoop
with a FAKE LLMClient (deterministic, no API key).

Exercises: streaming chunks, tool round-trip, and cross-turn memory —
the roadmap Phase 1 / M2 acceptance, headlessly.
"""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.server import make_handler
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.registry import ToolRegistry
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _reg_with_echo():
    reg = ToolRegistry()

    async def echo(text: str) -> str:
        return f"TOOL:{text}"

    reg.register(
        "echo", "echo",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo,
    )
    return reg


def _build_loop(scripts):
    return AgentLoop(_ScriptedLLM(scripts), SessionStore(), _reg_with_echo(), LongTermMemory())


async def _collect_streamed(browser) -> tuple[str, bool]:
    """Collect chat.delta into chat.final. Returns (assembled, saw_final)."""
    assembled = ""
    saw_final = False
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(browser.recv(), timeout=5)
        frame = json.loads(raw)
        if frame["type"] != "event":
            continue
        if frame["event"] == "chat.delta":
            assembled += frame["payload"]["content"]
        elif frame["event"] == "chat.final":
            if frame["payload"].get("content"):
                assembled = frame["payload"]["content"]
            saw_final = True
            break
    return assembled, saw_final


def test_end_to_end_tool_round_trip(port_factory) -> None:
    agentserver_port = port_factory()
    gateway_port = port_factory()
    scripts = [
        # turn 1: model calls echo tool, then answers
        [Finish("tool_calls", {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "ping"}'}}]})],
        [TextDelta("answer:"), TextDelta("TOOL:ping"),
         Finish("stop", {"role": "assistant", "content": "answer:TOOL:ping", "tool_calls": None})],
    ]
    loop_obj = _build_loop(scripts)

    async def run() -> None:
        server = await serve(make_handler(loop_obj), "127.0.0.1", agentserver_port)
        try:
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()

            channel_manager = ChannelManager()
            message_handler = MessageHandler(agent_client, channel_manager)
            channel_manager.set_message_handler(message_handler)
            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()
            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    await browser.recv()  # connection.ack
                    await browser.send(json.dumps({
                        "type": "req", "id": "r1", "method": "chat.send",
                        "params": {"query": "call echo", "session_id": "s1"},
                    }))
                    ack = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
                    assert ack["type"] == "res" and ack["ok"] is True
                    assembled, saw_final = await _collect_streamed(browser)
                    assert saw_final
                    assert "answer:TOOL:ping" in assembled
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all green (session_store, memory, tool_registry, web_fetch, web_search, llm_client, agent_loop, agentserver_handler, integration).

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "Phase 1: E2E integration with real loop + fake LLM (tool round-trip)"
```

---

### Task 12: Manual smoke + docs

The deterministic tests use a fake LLM. A real-model smoke confirms the actual provider wiring. This is the human-facing M2 acceptance.

**Files:**
- Modify: `docs/phase-0-design.md` (add a Phase 1 note) — optional, skip if `docs/phase-1-design.md` already covers it. Actually: create no new doc; the spec is the design doc. Just run the smoke.

- [ ] **Step 1: Manual real-model smoke**

Set env (point at your OpenAI-compatible endpoint):

```bash
export TWINKLE_LLM_BASE_URL=https://api.openai.com/v1
export TWINKLE_LLM_API_KEY=sk-...
export TWINKLE_LLM_MODEL=gpt-4o-mini
python scripts/start_services.py &
cd web && npm run dev
```

Open http://localhost:5173. Verify:
1. Ask "fetch https://example.com and summarize" → model calls `web_fetch`, result integrates into answer.
2. Ask a follow-up "what did it say the domain was?" → model remembers prior turn (cross-turn memory).
3. Ask "search the web for twinkle" → model calls `web_search`.

Expected: streamed tokens appear live; tool calls fire; follow-up references prior context.

- [ ] **Step 2: Commit any doc touch-ups**

If you updated the spec or README to reflect Phase 1 wiring, commit:

```bash
git add -A
git commit -m "Phase 1: docs touch-ups for agent loop wiring"
```

If nothing to commit, skip.

---

## Self-Review (run after writing — already done)

**1. Spec coverage:**
- §2 LLM 提供方 OpenAI 兼容 → Task 1 (config) + Task 7 (LLMClient). ✓
- §2 会话记录归 AgentServer → Task 2 (SessionStore) + Task 8 (loop appends) + Task 9 (server owns loop/store). ✓
- §2 session_id 浏览器造 → Task 10. ✓
- §2 流式接缝 async generator → Task 8 `run_stream` + Task 9 `make_handler` consume. ✓
- §2 工具 web_fetch + web_search → Tasks 5, 6, 9 registry. ✓
- §2 存储 in-memory → Task 2. ✓
- §2 长期记忆 stub → Task 3, called in Task 8. ✓
- §4 五个接口 → Tasks 2,3,4,7,8. ✓
- §5 闭环轨迹 → Task 8 implements exact steps. ✓
- §6 slim 重写 + 明确不迁 → Tasks 5,6 implement slim; command/memory/todo omitted. ✓
- §7 max_steps=8 → Task 8. ✓
- §8 测试 (loop 单测无需 ws, E2E 多轮+工具) → Tasks 8, 11. ✓
- §9 明确不做 → respected (no dynamic registry, no compression, no command/memory/todo, no persistence, no Jina/paid). ✓

**2. Placeholder scan:** No TBD/TODO. All code blocks complete. Task 9 Step 4 gives a single authoritative `server.py` (asyncio imported at top; no bare `handler` symbol).

**3. Type consistency:**
- `SessionStore.get_messages` / `append` — same in Tasks 2, 8, 9. ✓
- `LongTermMemory.recall`/`store` — Task 3 defines; Task 8 calls `recall(query)`. ✓
- `ToolRegistry.register`/`schemas`/`execute` — Tasks 4, 5, 6, 8, 9, 11 consistent. ✓
- `LLMClient.stream(messages, tools)` returning `TextDelta`/`Finish` — Tasks 7, 8 consistent; `Finish` fields `finish_reason`/`assistant_message` consistent across 7, 8, 11. ✓
- `AgentLoop(llm, store, tools, memory)` + `run_stream(env)`/`run_unary(env)` — Tasks 8, 9, 11 consistent. ✓
- `make_handler(loop)` / `build_default_loop()` — Tasks 9, 11 consistent. ✓
- `E2AResponse` fields (`request_id`,`sequence`,`is_final`,`status`,`response_kind`,`body`) — Task 8 matches `twinkle/e2a/models.py`. ✓

No issues found.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-17-phase1-agent-loop.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
