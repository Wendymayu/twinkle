# Session Management & History Viewing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users create new chat sessions, list past sessions in a sidebar, and view a past session's conversation history — with disk persistence so sessions survive server restarts.

**Architecture:** AgentServer owns session RPCs and a disk-backed `SessionStore` (JSON files per session, mirroring jiuwenclaw). A new `e2a.result` E2A frame kind carries structured RPC responses; the gateway maps it 1:1 to a new browser `result` event. `chat.send` streaming is unchanged. The frontend uses a module-level reactive composable (`useSessions.ts`, no Pinia) and a new `SessionSidebar` component; `session_id` is sticky in `localStorage`.

**Tech Stack:** Python 3 (stdlib `json`/`pathlib`/`asyncio`, `pydantic`), `websockets`, Vue 3 + Vite (no Pinia/Router).

**Spec:** `docs/superpowers/specs/2026-07-22-session-management-design.md`

## Global Constraints

- Tests use plain `def test_*` + `asyncio.run()`. **No `pytest-asyncio`** (deliberate project choice).
- New `SessionStore(sessions_dir)` constructor takes a directory path (str/Path). No default — production passes `SESSIONS_DIR`; tests pass a tmp path.
- Mutating `SessionStore` methods (`append`, `create_session`, `delete_session`) are `async` and hold a single `asyncio.Lock`. Read methods (`get_messages`, `list_sessions`, `get_history`) are sync.
- Session files live under `<repo_root>/.twinkle_data/sessions/<sid>/` (gitignored). Each session: `metadata.json` + `history.json` (JSONL).
- `history.json` records preserve full OpenAI-native fields (`tool_calls`, `tool_call_id`) so the ReAct context can be reconstructed on cold start.
- Gateway stays a pure format-translator — all session business logic lives in AgentServer.
- Broadcast-to-all leak in `WebChannel.send` is **intentionally not fixed** (out of scope).

## File Structure

**Backend (Python):**
- `twinkle/config.py` — add `SESSIONS_DIR`.
- `twinkle/agentserver/session_store.py` — rewrite: disk-backed cache+JSON, `create/list/delete/get_history`, cold-start hydration, auto-title.
- `twinkle/agentserver/agent_loop.py` — 4 `append` call sites become `await` + pass `request_id`.
- `twinkle/agentserver/session_rpc.py` — **new**: `_dispatch_session_rpc` router for `session.*/history.get`.
- `twinkle/agentserver/server.py` — `ws_handler(loop, store)` + method routing; `agent_loop()` builds the store.
- `twinkle/e2a/models.py` — `response_kind` docstring gains `e2a.result`.
- `twinkle/schema/message.py` — `EventType.RESULT = "result"`.
- `twinkle/gateway/message_handler.py` — `_process_stream` adds `e2a.result → result` branch.

**Frontend (Vue/TS):**
- `web/src/services/webClient.ts` — expose `sessionId`, sticky `localStorage`, `request(method)`.
- `web/src/composables/useSessions.ts` — **new**: session state singleton.
- `web/src/components/SessionSidebar.vue` — **new**.
- `web/src/components/ChatPanel.vue` — **new** (extracted from `App.vue`).
- `web/src/components/TodoPanel.vue` — **new** (extracted from `App.vue`).
- `web/src/App.vue` — layout shell.

**Tests + docs:**
- `tests/conftest.py` — `sessions_dir` + `session_store` fixtures.
- `tests/test_session_store.py` — migrate + extend.
- `tests/test_agent_loop.py` — migrate `SessionStore()` → fixture.
- `tests/test_message_handler.py` — extend.
- `tests/test_session_rpc.py` — **new**.
- `.gitignore`, `docs/architecture.md`, `CLAUDE.md`.

---

### Task 1: Config + .gitignore + conftest fixtures

**Files:**
- Modify: `twinkle/config.py`
- Modify: `.gitignore`
- Modify: `tests/conftest.py`
- Test: `tests/conftest.py`

**Interfaces:**
- Produces: `SESSIONS_DIR` (str) in `twinkle.config`; `sessions_dir` (Path) and `session_store` (`SessionStore`) pytest fixtures.

- [ ] **Step 1: Add `SESSIONS_DIR` to config**

Append to `twinkle/config.py` after the `WORKSPACE_DIR` block:

```python
# --- Sessions persistence (disk-backed session store) ---
# Per-session dir layout: <SESSIONS_DIR>/<session_id>/{metadata.json,history.json}.
# Defaults to <repo_root>/.twinkle_data/sessions (gitignored). If you want strict
# isolation from the command_exec sandbox (workdir confined under WORKSPACE_DIR),
# point this outside WORKSPACE_DIR.
SESSIONS_DIR = os.getenv("TWINKLE_SESSIONS_DIR") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "sessions"
)
```

- [ ] **Step 2: Add `.twinkle_data/` to `.gitignore`**

Append to `.gitignore`:

```
# Session persistence (disk-backed sessions, per-user local data)
.twinkle_data/
```

- [ ] **Step 3: Add fixtures to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def sessions_dir(tmp_path) -> "Path":
    """A fresh per-test sessions directory (disk-backed SessionStore target)."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def session_store(sessions_dir):
    """A SessionStore rooted in a per-test tmp dir (no repo pollution)."""
    from twinkle.agentserver.session_store import SessionStore
    return SessionStore(str(sessions_dir))
```

Add `from pathlib import Path` to the imports at the top of `conftest.py`.

- [ ] **Step 4: Verify fixtures resolve**

Run: `python -m pytest tests/conftest.py --co -q 2>&1 | head` — no collection errors. (The fixtures import `SessionStore`, which still has the old signature until Task 2; `session_store` is only resolved when a test requests it, so collection passes.)

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add twinkle/config.py .gitignore tests/conftest.py
git commit -m "config: add SESSIONS_DIR + sessions_dir/session_store test fixtures"
```

---

### Task 2: SessionStore disk core — create, append, get_messages, auto-title

**Files:**
- Modify: `twinkle/agentserver/session_store.py`
- Modify: `tests/test_session_store.py` (migrate existing + add disk tests)

**Interfaces:**
- Produces: `SessionStore(sessions_dir: str|Path)`; `async append(session_id, message, request_id=None, event_type=None)`; `get_messages(session_id) -> list[dict]`; `async create_session(session_id, channel_id="web") -> dict`. `get_messages` cold-hydrates from `history.json` on cache miss, reconstructing OpenAI messages including `tool_calls`/`tool_call_id`.

- [ ] **Step 1: Write the failing tests (migrate + extend)**

Replace the entire contents of `tests/test_session_store.py`:

```python
import asyncio
import json
from pathlib import Path

from twinkle.agentserver.session_store import SessionStore


def _run(coro):
    return asyncio.run(coro)


def test_append_and_get_round_trip(session_store):
    _run(session_store.append("s1", {"role": "user", "content": "hi"}))
    _run(session_store.append("s1", {"role": "assistant", "content": "hello"}))
    msgs = session_store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_sessions_are_isolated(session_store):
    _run(session_store.append("s1", {"role": "user", "content": "a"}))
    _run(session_store.append("s2", {"role": "user", "content": "b"}))
    assert [m["content"] for m in session_store.get_messages("s1")] == ["a"]
    assert [m["content"] for m in session_store.get_messages("s2")] == ["b"]


def test_unknown_session_returns_empty(session_store):
    assert session_store.get_messages("never") == []


def test_create_session_writes_metadata(session_store, sessions_dir):
    meta = _run(session_store.create_session("s1"))
    mpath = Path(sessions_dir) / "s1" / "metadata.json"
    assert mpath.is_file()
    on_disk = json.loads(mpath.read_text(encoding="utf-8"))
    assert on_disk["session_id"] == "s1"
    assert on_disk["title"] == ""
    assert on_disk["message_count"] == 0
    assert meta["session_id"] == "s1"


def test_create_session_is_idempotent(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    # second call must not error or reset an existing populated metadata
    _run(session_store.create_session("s1"))
    on_disk = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert on_disk["message_count"] == 0


def test_append_writes_history_line_and_updates_metadata(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hello"},
                              request_id="r1"))
    hpath = Path(sessions_dir) / "s1" / "history.json"
    lines = [json.loads(l) for l in hpath.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["role"] == "user"
    assert lines[0]["content"] == "hello"
    assert lines[0]["request_id"] == "r1"
    assert lines[0]["session_id"] == "s1"
    meta = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert meta["message_count"] == 1
    assert meta["last_message_at"] >= meta["created_at"]


def test_first_user_message_auto_titles(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    long_msg = "x" * 80
    _run(session_store.append("s1", {"role": "user", "content": long_msg},
                              request_id="r1"))
    meta = json.loads((Path(sessions_dir) / "s1" / "metadata.json").read_text())
    assert meta["title"].startswith("x" * 50)
    assert meta["title"].endswith("...")


def test_append_preserves_tool_calls_for_react(session_store):
    _run(session_store.create_session("s1"))
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]
    _run(session_store.append("s1", {"role": "assistant", "content": None,
                                    "tool_calls": tc}, request_id="r1"))
    _run(session_store.append("s1", {"role": "tool", "tool_call_id": "c1",
                                    "content": "tool-saw:hi"}, request_id="r1"))
    msgs = session_store.get_messages("s1")
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["tool_calls"] == tc
    assert msgs[-1]["role"] == "tool"
    assert msgs[-1]["tool_call_id"] == "c1"


def test_cold_start_hydrates_full_history(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "system", "content": "sys"}))
    _run(session_store.append("s1", {"role": "user", "content": "q"},
                              request_id="r1"))
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "echo", "arguments": '{}'}}]
    _run(session_store.append("s1", {"role": "assistant", "content": None,
                                      "tool_calls": tc}, request_id="r1"))
    _run(session_store.append("s1", {"role": "tool", "tool_call_id": "c1",
                                    "content": "res"}, request_id="r1"))

    # Brand-new store instance pointing at the SAME dir — cache is cold.
    cold = SessionStore(str(sessions_dir))
    msgs = cold.get_messages("s1")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["tool_calls"] == tc
    assert msgs[3]["tool_call_id"] == "c1"
    assert msgs[3]["content"] == "res"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: FAIL — `SessionStore()` takes no `sessions_dir` yet; methods missing.

- [ ] **Step 3: Rewrite `session_store.py`**

Replace the entire file:

```python
"""Disk-backed short-term session memory.

Per-session layout under ``sessions_dir``::

    <sessions_dir>/<session_id>/
        metadata.json   # {session_id, title, created_at, last_message_at, ...}
        history.json    # JSONL, one record per appended message

Two layers: an in-memory cache (``dict[sid -> list[OpenAI msg]]``) for the
AgentLoop's hot reads, plus on-disk JSON for persistence across restarts.
``get_messages`` cold-hydrates from ``history.json`` on a cache miss so a ReAct
turn can resume with full prior context (system prompt, tool_calls, tool results).

Mirrors jiuwenclaw's ``session_metadata.py`` + ``session_history.py`` (file-per-
session, JSONL history, auto-title from first user message), minus jiuwenclaw's
async write-queue — Twinkle is single-user single-process, so a single
``asyncio.Lock`` serializing metadata read-modify-write is enough.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("twinkle.agentserver.session_store")

_TITLE_MAX_LEN = 50
_OPENAI_FIELDS = ("role", "content", "tool_calls", "tool_call_id")


def _auto_title(content: str) -> str:
    title = (content or "").strip().replace("\n", " ")
    if len(title) > _TITLE_MAX_LEN:
        return title[:_TITLE_MAX_LEN] + "..."
    return title


class SessionStore:
    def __init__(self, sessions_dir: str | Path) -> None:
        self._root = Path(sessions_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    # --- paths ---

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _metadata_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "metadata.json"

    def _history_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "history.json"

    # --- session lifecycle ---

    async def create_session(self, session_id: str, channel_id: str = "web") -> dict:
        """Idempotently create a session dir + metadata. Existing metadata is
        left untouched (a re-create never wipes a populated session)."""
        async with self._lock:
            sdir = self._session_dir(session_id)
            sdir.mkdir(parents=True, exist_ok=True)
            mpath = self._metadata_path(session_id)
            if mpath.is_file():
                try:
                    return json.loads(mpath.read_text(encoding="utf-8"))
                except Exception:
                    pass  # corrupt — fall through and rewrite defaults
            now = time.time()
            meta = {
                "session_id": session_id,
                "title": "",
                "created_at": now,
                "last_message_at": now,
                "message_count": 0,
                "channel_id": channel_id,
            }
            mpath.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            return meta

    async def delete_session(self, session_id: str) -> bool:
        """Remove a session dir + evict the cache entry. Returns False if absent."""
        import shutil
        async with self._lock:
            sdir = self._session_dir(session_id)
            if not sdir.exists():
                self._cache.pop(session_id, None)
                return False
            shutil.rmtree(sdir, ignore_errors=True)
            self._cache.pop(session_id, None)
            return True

    def list_sessions(self, limit: int = 100) -> list[dict]:
        """List sessions sorted by last_message_at desc. Corrupt/missing
        metadata falls back to dir mtime (mirrors jiuwenclaw legacy fallback)."""
        out: list[dict] = []
        if not self._root.exists():
            return out
        for sdir in self._root.iterdir():
            if not sdir.is_dir():
                continue
            mpath = sdir / "metadata.json"
            try:
                meta = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                st = sdir.stat()
                meta = {
                    "session_id": sdir.name,
                    "title": "(无标题)",
                    "created_at": st.st_ctime,
                    "last_message_at": st.st_mtime,
                    "message_count": 0,
                    "channel_id": "web",
                }
            meta.setdefault("session_id", sdir.name)
            out.append(meta)
        out.sort(key=lambda m: m.get("last_message_at", 0), reverse=True)
        return out[:limit]

    def get_history(self, session_id: str) -> list[dict]:
        """Return raw history records for frontend display (newest last).
        Bad JSONL lines are skipped, never raised."""
        hpath = self._history_path(session_id)
        if not hpath.is_file():
            return []
        out: list[dict] = []
        for line in hpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping corrupt history line in %s", session_id)
        return out

    # --- message store (AgentLoop-facing) ---

    def get_messages(self, session_id: str) -> list[dict]:
        """Return OpenAI-native messages for the ReAct loop. Cache hit returns
        immediately; cache miss cold-hydrates from history.json."""
        cached = self._cache.get(session_id)
        if cached is not None:
            return list(cached)
        msgs = [self._record_to_openai(r) for r in self.get_history(session_id)]
        self._cache[session_id] = msgs
        return list(msgs)

    async def append(
        self,
        session_id: str,
        message: dict,
        request_id: str | None = None,
        event_type: str | None = None,
    ) -> None:
        """Append a message: update the in-memory cache, append a history.json
        record, and update metadata (count, last_message_at, auto-title on the
        first user message)."""
        async with self._lock:
            # ensure the session exists on disk (implicit create)
            sdir = self._session_dir(session_id)
            if not sdir.is_dir():
                await self.create_session(session_id)
            # cache
            self._cache.setdefault(session_id, []).append(dict(message))
            # history record (preserve full OpenAI fields for cold reconstruction)
            role = message.get("role")
            record = {
                "id": f"{request_id or 'none'}:{role}",
                "role": role,
                "request_id": request_id,
                "channel_id": "web",
                "timestamp": time.time(),
                "content": message.get("content"),
                "event_type": event_type,
                "session_id": session_id,
                "tool_calls": message.get("tool_calls"),
                "tool_call_id": message.get("tool_call_id"),
            }
            with self._history_path(session_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            # metadata update
            self._update_metadata(session_id, role, message.get("content"))

    def _update_metadata(self, session_id: str, role: str | None, content: Any) -> None:
        mpath = self._metadata_path(session_id)
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            now = time.time()
            meta = {
                "session_id": session_id, "title": "",
                "created_at": now, "last_message_at": now,
                "message_count": 0, "channel_id": "web",
            }
        meta["message_count"] = int(meta.get("message_count", 0)) + 1
        meta["last_message_at"] = time.time()
        if not meta.get("title") and role == "user":
            meta["title"] = _auto_title(content if isinstance(content, str) else "")
        mpath.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _record_to_openai(record: dict) -> dict:
        """Reconstruct an OpenAI-native message from a history record, dropping
        None-valued optional fields."""
        msg: dict[str, Any] = {}
        for k in _OPENAI_FIELDS:
            v = record.get(k)
            if v is not None:
                msg[k] = v
        return msg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/session_store.py tests/test_session_store.py
git commit -m "session: disk-backed SessionStore (create/append/hydrate/auto-title)"
```

---

### Task 3: SessionStore list/delete/get_history edge cases

**Files:**
- Modify: `tests/test_session_store.py` (append more tests)

**Interfaces:**
- Produces: `list_sessions`, `delete_session`, `get_history` verified for ordering, fallback, deletion, and corrupt-line tolerance.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_session_store.py`:

```python
def test_list_sessions_sorted_desc(session_store, sessions_dir):
    _run(session_store.create_session("old"))
    _run(session_store.append("old", {"role": "user", "content": "a"},
                              request_id="r1"))
    # tiny sleep-free ordering: old was created first -> lower last_message_at
    _run(session_store.create_session("new"))
    _run(session_store.append("new", {"role": "user", "content": "b"},
                              request_id="r2"))
    rows = session_store.list_sessions()
    assert [r["session_id"] for r in rows] == ["new", "old"]


def test_list_sessions_falls_back_on_corrupt_metadata(session_store, sessions_dir):
    sdir = Path(sessions_dir) / "broken"
    sdir.mkdir()
    (sdir / "metadata.json").write_text("{not valid json", encoding="utf-8")
    rows = session_store.list_sessions()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "broken"
    assert rows[0]["title"] == "(无标题)"


def test_delete_session_removes_dir_and_evicts_cache(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}))
    assert _run(session_store.delete_session("s1")) is True
    assert not (Path(sessions_dir) / "s1").exists()
    # cache evicted -> cold read returns empty
    assert session_store.get_messages("s1") == []
    # deleting again -> False (absent)
    assert _run(session_store.delete_session("s1")) is False


def test_get_history_skips_corrupt_lines(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    hpath = Path(sessions_dir) / "s1" / "history.json"
    hpath.write_text(
        json.dumps({"role": "user", "content": "good"}) + "\n"
        + "{bad line\n"
        + json.dumps({"role": "assistant", "content": "ok"}) + "\n",
        encoding="utf-8",
    )
    rows = session_store.get_history("s1")
    assert [r["content"] for r in rows] == ["good", "ok"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_store.py -k "list_sessions_sorted or falls_back or delete_session_removes or skips_corrupt" -v`
Expected: the new tests should mostly PASS already (Task 2 implemented them); if any FAIL, fix.

- [ ] **Step 3: If any fail, fix in `session_store.py`; re-run**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_session_store.py
git commit -m "session: tests for list/delete/history edge cases + corrupt-line tolerance"
```

---

### Task 4: AgentLoop — await append + pass request_id

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py:55-59,80,100-107`
- Modify: `tests/test_agent_loop.py` (migrate `SessionStore()` → fixture)

**Interfaces:**
- Consumes: `SessionStore` from Task 2 (`async append`, sync `get_messages`).
- Produces: `AgentLoop.run_stream` now `await`s `append` and threads `envelope.request_id`. Existing ReAct behavior unchanged.

- [ ] **Step 1: Migrate `tests/test_agent_loop.py` to the fixture**

Each `test_*` function currently does `store = SessionStore()`. Change the signature to accept the `session_store` fixture and drop the local construction. For each test function, e.g.:

```python
def test_plain_answer_streams_chunks_and_complete(session_store) -> None:
    store = session_store
    llm = _ScriptedLLM([...])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())
    ...
```

Apply the same `def test_*(session_store):` + `store = session_store` change to **all** test functions in `tests/test_agent_loop.py` (`test_plain_answer...`, `test_tool_call_round_trip...`, `test_cross_turn_remembers_context`, `test_max_steps_emits_error`, `test_todo_create_round_trip...`, `test_todo_update_frame_emitted_on_create`). Remove the now-unused `from twinkle.agentserver.session_store import SessionStore` import only if nothing else uses it (it isn't — drop it).

- [ ] **Step 2: Run agent_loop tests to verify they fail**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL — `append` is now async and not awaited inside `run_stream` → coroutine never awaited / TypeError.

- [ ] **Step 3: Update `agent_loop.py` append call sites**

In `twinkle/agentserver/agent_loop.py`, change:

- line ~57: `self._store.append(session_id, {"role": "system", "content": TODO_SYSTEM_PROMPT})`
  → `await self._store.append(session_id, {"role": "system", "content": TODO_SYSTEM_PROMPT}, request_id=envelope.request_id)`
- line ~59: `self._store.append(session_id, {"role": "user", "content": query})`
  → `await self._store.append(session_id, {"role": "user", "content": query}, request_id=envelope.request_id)`
- line ~80: `self._store.append(session_id, ev.assistant_message)`
  → `await self._store.append(session_id, ev.assistant_message, request_id=envelope.request_id, event_type="chat.final")`
- line ~100-107 (the tool-result append):
  ```python
  await self._store.append(
      session_id,
      {"role": "tool", "tool_call_id": tc["id"], "content": result},
      request_id=envelope.request_id,
      event_type="chat.tool_result",
  )
  ```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: all 6 tests PASS (ReAct semantics unchanged; tool_calls/tool_call_id still asserted because the cache holds the full dict).

- [ ] **Step 5: Run the touched suites**

Run: `python -m pytest tests/test_agent_loop.py tests/test_session_store.py tests/test_message_handler.py -v`
Expected: PASS. (Note: `tests/test_integration.py` still uses the old `SessionStore()` no-arg + `ws_handler(loop)` one-arg shape and will fail until Task 6 fixes it — do **not** run it in this task.)

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/agent_loop.py tests/test_agent_loop.py
git commit -m "agent_loop: await SessionStore.append + thread request_id/event_type"
```

---

### Task 5: E2A `e2a.result` + `EventType.RESULT` + MessageHandler mapping

**Files:**
- Modify: `twinkle/e2a/models.py` (docstring)
- Modify: `twinkle/schema/message.py`
- Modify: `twinkle/gateway/message_handler.py`
- Modify: `tests/test_message_handler.py`

**Interfaces:**
- Produces: `EventType.RESULT = "result"`; `MessageHandler._process_stream` maps `E2AResponse(response_kind="e2a.result")` → `Message(event_type=EventType.RESULT, payload=resp.body)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_message_handler.py`:

```python
def test_result_frame_becomes_result_event() -> None:
    body = {"type": "session.list", "sessions": [{"session_id": "s1", "title": "t"}]}
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=True,
            status="succeeded", response_kind="e2a.result", body=body,
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="session.list", params={})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.RESULT
    assert out.payload == body
    assert out.content == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_message_handler.py::test_result_frame_becomes_result_event -v`
Expected: FAIL — `EventType.RESULT` missing / `e2a.result` currently falls into the `else` branch and becomes an empty `chat.final`.

- [ ] **Step 3: Add `EventType.RESULT`**

In `twinkle/schema/message.py`, add to the `EventType` enum:

```python
    RESULT = "result"
```

- [ ] **Step 4: Add the `e2a.result` branch in `MessageHandler`**

In `twinkle/gateway/message_handler.py`, in `_process_stream`, add a branch **before** the `else` (so result frames don't get treated as chat). Replace the existing `if resp.response_kind == "e2a.todo_update":` block's sibling structure so it reads:

```python
        async for resp in self._agent_client.send_request_stream(envelope):
            if resp.response_kind == "e2a.todo_update":
                out = Message(
                    id=msg.id, type="event", channel_id=msg.channel_id,
                    session_id=msg.session_id,
                    event_type=EventType.TODO_UPDATE,
                    payload=dict(resp.body), content="",
                )
            elif resp.response_kind == "e2a.result":
                out = Message(
                    id=msg.id, type="event", channel_id=msg.channel_id,
                    session_id=msg.session_id,
                    event_type=EventType.RESULT,
                    payload=dict(resp.body), content="",
                )
            else:
                content = (resp.body.get("result") or {}).get("content", "")
                event_type = EventType.CHAT_FINAL if resp.is_final else EventType.CHAT_DELTA
                out = Message(
                    id=msg.id, type="event", channel_id=msg.channel_id,
                    session_id=msg.session_id,
                    event_type=event_type,
                    content=content,
                )
            await self.enqueue_outbound(out)
```

- [ ] **Step 5: Update the `E2AResponse` docstring**

In `twinkle/e2a/models.py`, change the `response_kind` comment:

```python
    response_kind: str = "e2a.chunk"  # e2a.chunk | e2a.complete | e2a.error | e2a.todo_update | e2a.result
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_message_handler.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add twinkle/e2a/models.py twinkle/schema/message.py twinkle/gateway/message_handler.py tests/test_message_handler.py
git commit -m "e2a: add e2a.result frame -> result browser event mapping in gateway"
```

---

### Task 6: Session RPC dispatch (`session_rpc.py` + `server.py` routing)

**Files:**
- Create: `twinkle/agentserver/session_rpc.py`
- Modify: `twinkle/agentserver/server.py`
- Create: `tests/test_session_rpc.py`

**Interfaces:**
- Consumes: `SessionStore` (Task 2: `create_session`/`list_sessions`/`delete_session`/`get_history`), `E2AEnvelope`/`E2AResponse`.
- Produces: `dispatch_session_rpc(envelope, store) -> AsyncIterator[E2AResponse]` (single `e2a.result` frame); `ws_handler(loop, store)` routes by `envelope.method`; `agent_loop()` builds the store from `SESSIONS_DIR`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_rpc.py`:

```python
import asyncio

import pytest

from twinkle.agentserver.session_rpc import dispatch_session_rpc
from twinkle.e2a.models import E2AEnvelope


def _env(method, rid="r1", session_id="s1", params=None):
    return E2AEnvelope(
        request_id=rid, session_id=session_id,
        method=method, params=params or {},
    )


def _run(coro):
    return asyncio.run(coro)


async def _frames(envelope, store):
    return [f async for f in dispatch_session_rpc(envelope, store)]


def test_session_list_returns_result_frame(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hello"},
                              request_id="r1"))
    frames = _run(_frames(_env("session.list"), session_store))
    assert len(frames) == 1
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.is_final is True
    assert f.status == "succeeded"
    assert f.request_id == "r1"
    assert f.body["type"] == "session.list"
    assert [s["session_id"] for s in f.body["sessions"]] == ["s1"]
    assert f.body["sessions"][0]["title"] == "hello"


def test_session_create_returns_result_frame(session_store, sessions_dir):
    frames = _run(_frames(
        _env("session.create", session_id="s-new", params={"session_id": "s-new"}),
        session_store,
    ))
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.body["type"] == "session.create"
    assert f.body["session_id"] == "s-new"
    assert (sessions_dir / "s-new" / "metadata.json").is_file()


def test_history_get_returns_messages(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"},
                              request_id="r1"))
    _run(session_store.append("s1", {"role": "assistant", "content": "yo"},
                              request_id="r1"))
    frames = _run(_frames(
        _env("history.get", session_id="s1"), session_store,
    ))
    f = frames[0]
    assert f.body["type"] == "history.get"
    roles = [m["role"] for m in f.body["messages"]]
    assert roles == ["user", "assistant"]
    assert f.body["messages"][0]["content"] == "hi"


def test_session_delete_removes_and_returns_result(session_store, sessions_dir):
    _run(session_store.create_session("s1"))
    frames = _run(_frames(
        _env("session.delete", session_id="s1"), session_store,
    ))
    f = frames[0]
    assert f.body["type"] == "session.delete"
    assert f.body["session_id"] == "s1"
    assert not (sessions_dir / "s1").exists()


def test_unknown_session_method_returns_no_frames(session_store):
    # dispatch_session_rpc only handles session.*/history.get; an unknown
    # method yields nothing (the caller — server.py — falls through to the
    # AgentLoop for chat.send). We assert it yields no frames for a method
    # it does not own.
    frames = _run(_frames(_env("chat.send"), session_store))
    assert frames == []


def test_history_get_unknown_session_returns_empty(session_store):
    frames = _run(_frames(_env("history.get", session_id="nope"), session_store))
    f = frames[0]
    assert f.body["messages"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_rpc.py -v`
Expected: FAIL — `session_rpc` module doesn't exist.

- [ ] **Step 3: Create `session_rpc.py`**

Create `twinkle/agentserver/session_rpc.py`:

```python
"""Dispatch table for session/history RPCs at the AgentServer.

These are the RPC methods Twinkle originally dropped (``session.list`` /
``history.get`` were marked "roadmap 不做" in docs/e2a-introduction.md) —
re-adopted here, mirroring jiuwenclaw's remote storage mode where the agent
server (not the gateway) owns session business logic.

Each handler yields a single ``E2AResponse`` with ``response_kind="e2a.result"``
and ``is_final=True``; the gateway maps that to the browser ``result`` event.
On failure it yields a ``status="failed"`` result frame with an ``error`` body
so the frontend ``request()`` can reject cleanly.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from twinkle.agentserver.session_store import SessionStore
from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.agentserver.session_rpc")

_SESSION_METHODS = {"session.create", "session.list", "session.delete", "history.get"}


def handles(method: str) -> bool:
    return method in _SESSION_METHODS


async def dispatch_session_rpc(
    envelope: E2AEnvelope, store: SessionStore
) -> AsyncIterator[E2AResponse]:
    method = envelope.method
    sid = envelope.params.get("session_id") or envelope.session_id
    try:
        if method == "session.create":
            await store.create_session(sid)
            body = {"type": "session.create", "session_id": sid}
        elif method == "session.list":
            rows = store.list_sessions()
            body = {"type": "session.list", "sessions": rows}
        elif method == "session.delete":
            await store.delete_session(sid)
            body = {"type": "session.delete", "session_id": sid}
        elif method == "history.get":
            records = store.get_history(sid)
            body = {"type": "history.get", "messages": records}
        else:
            return  # not a session RPC — caller routes to AgentLoop
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=0,
            is_final=True,
            status="succeeded",
            response_kind="e2a.result",
            body=body,
        )
    except Exception as exc:
        log.exception("session rpc %s failed: %s", method, exc)
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=0,
            is_final=True,
            status="failed",
            response_kind="e2a.result",
            body={"type": method, "error": str(exc)},
        )
```

- [ ] **Step 4: Run RPC tests to verify they pass**

Run: `python -m pytest tests/test_session_rpc.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Wire routing into `server.py`**

In `twinkle/agentserver/server.py`:

- Add imports near the top:
  ```python
  from twinkle.agentserver.session_rpc import dispatch_session_rpc, handles as handles_session_rpc
  from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, SESSIONS_DIR
  ```
  (mergeve with the existing `from twinkle.config import ...` line — keep it one import line, just add `SESSIONS_DIR`).

- Change `agent_loop()` to build and return the store alongside the loop is not possible with the current single-return shape. Instead, add a `build_agent_loop()` that returns `(loop, store)`:

  ```python
  def build_agent_loop():
      """Production wiring — config-driven LLM + disk-backed SessionStore."""
      llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
      store = SessionStore(SESSIONS_DIR)
      tools = tool_manager()
      memory = LongTermMemory()
      return AgentLoop(llm, store, tools, memory), store
  ```

  Keep the old `agent_loop()` name as a thin shim so any existing caller still works:
  ```python
  def agent_loop() -> AgentLoop:
      loop, _ = build_agent_loop()
      return loop
  ```

- Change `ws_handler(loop: AgentLoop)` → `ws_handler(loop: AgentLoop, store: SessionStore)` and route in the handler body. Replace the `try:` block that calls `loop.run_stream`:

  ```python
  def ws_handler(loop: AgentLoop, store: SessionStore):
      """Return a ws handler bound to the given AgentLoop + SessionStore."""

      async def handler(ws) -> None:
          try:
              await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
          except Exception:
              return
          async for raw in ws:
              try:
                  envelope = E2AEnvelope.model_validate_json(raw)
              except Exception as exc:
                  err = E2AResponse(
                      request_id="?", status="failed",
                      response_kind="e2a.error", body={"error": str(exc)},
                  )
                  await _safe_send(ws, err)
                  continue
              try:
                  if handles_session_rpc(envelope.method):
                      async for frame in dispatch_session_rpc(envelope, store):
                          await _safe_send(ws, frame)
                  else:
                      async for frame in loop.run_stream(envelope):
                          await _safe_send(ws, frame)
              except Exception as exc:
                  log.exception("agent loop failed for %s: %s", envelope.request_id, exc)
                  err = E2AResponse(
                      request_id=envelope.request_id, status="failed",
                      response_kind="e2a.error", body={"error": str(exc)},
                  )
                  await _safe_send(ws, err)

      return handler
  ```

- Update `main()`:
  ```python
  async def main() -> None:
      loop, store = build_agent_loop()
      handler = ws_handler(loop, store)
      log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
      async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
          await asyncio.Future()  # run forever
  ```

- Add `from twinkle.agentserver.session_store import SessionStore` to the imports (it's already imported — confirm and keep).

- [ ] **Step 6: Update `tests/test_integration.py` for the new signatures**

`tests/test_integration.py` currently builds `SessionStore()` (no arg) and calls `ws_handler(loop_obj)` (one arg) — both broken by this task. Build ONE store (so chat + RPC share state, as production does) and pass it to both `AgentLoop` and `ws_handler`:

Replace `_build_loop` + the `loop_obj`/`server` lines in `test_end_to_end_tool_round_trip`:

```python
def test_end_to_end_tool_round_trip(tmp_path, port_factory) -> None:
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
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM(scripts), store, _reg_with_echo(), LongTermMemory())

    async def run() -> None:
        server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
        # ... rest unchanged ...
```

(Delete the now-unused `_build_loop` helper; inline the loop construction as shown. `tmp_path` is the builtin pytest fixture.)

- [ ] **Step 7: Run the full backend suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS (test_session_store + test_agent_loop + test_message_handler + test_session_rpc + test_integration).

- [ ] **Step 8: Commit**

```bash
git add twinkle/agentserver/session_rpc.py twinkle/agentserver/server.py tests/test_session_rpc.py tests/test_integration.py
git commit -m "agentserver: session_rpc dispatch + ws_handler method routing"
```

---

### Task 7: Frontend — `webClient.ts` (expose sessionId, sticky, `request`)

**Files:**
- Modify: `web/src/services/webClient.ts`

**Interfaces:**
- Produces: `WebClient.getSessionId()`/`setSessionId(id)`; `localStorage`-sticky session id on connect; `request(method, params): Promise<any>` that resolves on the matching `result` event by `request_id`.

**Note:** No frontend test framework in this project — verify manually with `npm run dev` in Task 9.

- [ ] **Step 1: Rewrite `webClient.ts`**

Replace the entire file:

```typescript
// Minimal WebSocket client: sends {type:req,id,method,params}, correlates
// streamed chat.delta / chat.final events by request_id, surfaces
// todo.update events, and resolves session/history RPCs via a `request()`
// promise that awaits the matching `result` event.

export type DeltaHandler = (delta: string, requestId: string) => void
export type FinalHandler = (text: string, requestId: string) => void
export type TodoUpdateHandler = (
  todo: { tasks: TodoTask[]; remaining: number; total: number },
  requestId: string,
) => void

export interface TodoTask {
  idx: number
  title: string
  status: 'waiting' | 'running' | 'completed'
  result: string
}

const SESSION_KEY = 'twinkle.sessionId'

export class WebClient {
  private ws: WebSocket | null = null
  private onDelta: DeltaHandler = () => {}
  private onFinal: FinalHandler = () => {}
  private onTodoUpdate: TodoUpdateHandler = () => {}
  private seq = 0
  private sessionId = ''
  private lastRequestId = ''
  private pending = new Map<string, (payload: any) => void>()

  connect(onReady: () => void): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws.onopen = () => {
      // sticky session id: reuse the one from localStorage so a page reload
      // reattaches to the same session (backend cache or cold-start hydration).
      const saved = localStorage.getItem(SESSION_KEY)
      this.sessionId = saved && saved.startsWith('sess_') ? saved : 'sess_' + crypto.randomUUID()
      localStorage.setItem(SESSION_KEY, this.sessionId)
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

  getSessionId(): string {
    return this.sessionId
  }

  setSessionId(id: string): void {
    this.sessionId = id
    localStorage.setItem(SESSION_KEY, id)
  }

  getLastRequestId(): string {
    return this.lastRequestId
  }

  private handle(frame: any): void {
    if (frame.type === 'event' && frame.event === 'connection.ack') return
    if (frame.type === 'res') return // immediate ack — nothing to surface
    if (frame.type === 'event') {
      const rid = frame.request_id
      const content = frame.payload?.content ?? ''
      if (frame.event === 'chat.delta') this.onDelta(content, rid)
      else if (frame.event === 'chat.final') this.onFinal(content, rid)
      else if (frame.event === 'todo.update') this.onTodoUpdate(frame.payload ?? { tasks: [], remaining: 0, total: 0 }, rid)
      else if (frame.event === 'result') {
        const resolve = this.pending.get(rid)
        if (resolve) {
          this.pending.delete(rid)
          resolve(frame.payload)
        }
      }
    }
  }

  setHandlers(onDelta: DeltaHandler, onFinal: FinalHandler, onTodoUpdate: TodoUpdateHandler): void {
    this.onDelta = onDelta
    this.onFinal = onFinal
    this.onTodoUpdate = onTodoUpdate
  }

  send(method: string, params: Record<string, any>): string {
    const id = 'req_' + Date.now().toString(36) + '_' + (this.seq++).toString(36)
    this.lastRequestId = id
    const fullParams = { ...params, session_id: this.sessionId }
    this.ws?.send(JSON.stringify({ type: 'req', id, method, params: fullParams }))
    return id
  }

  /** Fire an RPC (session.*/history.get) and resolve with the `result` payload. */
  request(method: string, params: Record<string, any> = {}): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = this.send(method, params)
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`timeout waiting for result: ${method}`))
      }, 15000)
      this.pending.set(id, (payload: any) => {
        clearTimeout(timer)
        if (payload?.error) reject(new Error(payload.error))
        else resolve(payload)
      })
    })
  }
}
```

- [ ] **Step 2: Type-check / build**

Run: `cd web && npm run build 2>&1 | tail -20` (or `npx tsc --noEmit` if a build step is configured).
Expected: no type errors. (App.vue still references the old shape but only via `client.connect`/`setHandlers`/`send`, all of which still exist — build should pass until Task 9 changes App.vue.)

- [ ] **Step 3: Commit**

```bash
git add web/src/services/webClient.ts
git commit -m "web: WebClient sticky sessionId + request() RPC promise"
```

---

### Task 8: Frontend — `useSessions` composable

**Files:**
- Create: `web/src/composables/useSessions.ts`

**Interfaces:**
- Consumes: `WebClient` from Task 7.
- Produces: a module-level reactive singleton with `sessions`, `currentSessionId`, `messages`, `connected`, `busy`, `loading`, and methods `init/loadSessions/createSession/selectSession/deleteSession/sendQuery`.

- [ ] **Step 1: Create the composable**

Create `web/src/composables/useSessions.ts`:

```typescript
import { ref, computed } from 'vue'
import { WebClient, type TodoTask } from '../services/webClient'

export interface SessionItem {
  session_id: string
  title: string
  last_message_at: number
  message_count: number
}
export interface ChatMsg {
  role: 'user' | 'assistant' | 'tool'
  content: string
}
interface TodoState { tasks: TodoTask[]; remaining: number; total: number }

const client = new WebClient()
const sessions = ref<SessionItem[]>([])
const currentSessionId = ref<string>('')
const messages = ref<ChatMsg[]>([])
const connected = ref(false)
const busy = ref(false)
const loading = ref(false)
const todo = ref<TodoState | null>(null)

const completedCount = computed(() =>
  todo.value ? todo.value.tasks.filter((t) => t.status === 'completed').length : 0,
)

function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '◐'
  return '○'
}

function fromHistory(records: any[]): ChatMsg[] {
  // system messages are the todo-guidance prompt — skip in the UI.
  return records
    .filter((r) => r.role !== 'system')
    .map((r) => ({ role: r.role, content: r.content ?? '' }))
}

async function loadSessions() {
  const payload = await client.request('session.list', {})
  sessions.value = payload?.sessions ?? []
}

async function selectSession(id: string) {
  loading.value = true
  client.setSessionId(id)
  currentSessionId.value = id
  try {
    const payload = await client.request('history.get', { session_id: id })
    messages.value = fromHistory(payload?.messages ?? [])
  } finally {
    loading.value = false
  }
}

async function createSession() {
  const id = 'sess_' + crypto.randomUUID()
  client.setSessionId(id)
  currentSessionId.value = id
  messages.value = []
  await client.request('session.create', { session_id: id })
  await loadSessions()
}

async function deleteSession(id: string) {
  await client.request('session.delete', { session_id: id })
  if (id === currentSessionId.value) {
    await createSession()
  }
  await loadSessions()
}

function sendQuery(q: string) {
  if (!q.trim() || !connected.value) return
  messages.value.push({ role: 'user', content: q })
  busy.value = true
  client.send('chat.send', { query: q })
}

function init() {
  client.connect(() => {
    connected.value = true
    client.setHandlers(
      (delta, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        if (last && last.role === 'assistant') last.content += delta
        else messages.value.push({ role: 'assistant', content: delta })
      },
      (text, rid) => {
        if (rid !== client.getLastRequestId()) return
        const last = messages.value[messages.value.length - 1]
        if (!last || last.role !== 'assistant') messages.value.push({ role: 'assistant', content: text })
        else if (!last.content) last.content = text
        busy.value = false
        loadSessions() // refresh to pick up a fresh auto-title
      },
      (t) => { todo.value = t },
    )
    const saved = client.getSessionId()
    loadSessions().then(() => {
      if (saved) selectSession(saved).catch(() => createSession())
      else createSession()
    })
  })
}

export function useSessions() {
  return {
    sessions, currentSessionId, messages, connected, busy, loading, todo,
    completedCount, box,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
  }
}
```

- [ ] **Step 2: Type-check**

Run: `cd web && npm run build 2>&1 | tail -20`
Expected: no type errors.

- [ ] **Step 3: Commit**

```bash
git add web/src/composables/useSessions.ts
git commit -m "web: useSessions composable (session list/current/messages state)"
```

---

### Task 9: Frontend — components + App.vue shell

**Files:**
- Create: `web/src/components/SessionSidebar.vue`
- Create: `web/src/components/ChatPanel.vue`
- Create: `web/src/components/TodoPanel.vue`
- Modify: `web/src/App.vue`

**Interfaces:**
- Consumes: `useSessions` from Task 8.

- [ ] **Step 1: Create `TodoPanel.vue`** (extracted from current App.vue)

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useSessions } from '../composables/useSessions'

const { todo, completedCount, box } = useSessions()
</script>

<template>
  <aside class="todo-panel">
    <div class="todo-head">
      <span>Todo</span>
      <span class="todo-count" v-if="todo">{{ completedCount }}/{{ todo.total }}</span>
    </div>
    <ul v-if="todo && todo.tasks.length" class="todo-list">
      <li v-for="t in todo.tasks" :key="t.idx" :class="['todo-item', t.status]">
        <span class="todo-box">{{ box(t.status) }}</span>
        <span class="todo-idx">{{ t.idx }}.</span>
        <span class="todo-title">{{ t.title }}</span>
        <span class="todo-result" v-if="t.result">{{ t.result }}</span>
      </li>
    </ul>
    <p v-else class="todo-empty">暂无任务</p>
  </aside>
</template>

<style scoped>
.todo-panel {
  width: 280px; flex: 0 0 280px; border-left: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (max-width: 640px) {
  .todo-panel { width: 100%; flex: 0 0 auto; border-left: 0; border-top: 1px solid #e2e8f0; max-height: 40%; }
}
.todo-head { display: flex; justify-content: space-between; padding: .9rem 1rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; }
.todo-count { color: #6366f1; }
.todo-list { list-style: none; margin: 0; padding: .5rem; overflow-y: auto; flex: 1; }
.todo-item { display: flex; align-items: baseline; gap: .35rem; padding: .35rem .25rem; font-size: .9rem; }
.todo-item.completed .todo-title { text-decoration: line-through; color: #94a3b8; }
.todo-box { width: 1.1em; text-align: center; color: #4f46d5; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-result { color: #64748b; font-size: .8rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
</style>
```

- [ ] **Step 2: Create `ChatPanel.vue`** (extracted + consumes composable)

```vue
<script setup lang="ts">
import { ref, computed, nextTick } from 'vue'
import { useSessions } from '../composables/useSessions'

const { messages, connected, busy, loading, sendQuery } = useSessions()
const input = ref('')
const logEl = ref<HTMLUListElement | null>(null)

function scrollDown() {
  nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight })
}
function send() {
  const q = input.value.trim()
  if (!q || !connected.value) return
  input.value = ''
  sendQuery(q)
  scrollDown()
}
</script>

<template>
  <div class="chat">
    <header>
      <span class="title">✨ Twinkle</span>
      <span class="status" :class="{ on: connected }">{{ connected ? '已连接' : '连接中…' }}</span>
    </header>
    <ul ref="logEl" class="log">
      <li v-for="(m, i) in messages" :key="i" :class="['row', m.role]">
        <div v-if="m.role === 'tool'" class="tool-line">{{ m.content }}</div>
        <div v-else class="bubble">{{ m.content }}</div>
      </li>
      <li v-if="busy" class="row assistant"><div class="bubble processing">处理中…</div></li>
      <li v-if="loading" class="row assistant"><div class="bubble processing">加载历史…</div></li>
    </ul>
    <footer>
      <input v-model="input" @keyup.enter="send" :disabled="!connected" placeholder="说点什么…" />
      <button @click="send" :disabled="!connected">发送</button>
    </footer>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; flex: 1; min-width: 0; background: #f8fafc;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #1e293b; }
header { display: flex; align-items: baseline; gap: .6rem; padding: .9rem 1rem; border-bottom: 1px solid #e2e8f0; background: #fff; }
.title { font-weight: 700; font-size: 1.05rem; }
.status { margin-left: auto; font-size: .8rem; color: #ef4444; }
.status.on { color: #10b981; }
.log { list-style: none; margin: 0; padding: 1rem; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: .55rem; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: 75%; padding: .55rem .85rem; border-radius: 16px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; box-shadow: 0 1px 2px rgba(15,23,42,.06); }
.row.user .bubble { background: #4f46d5; color: #fff; border-bottom-right-radius: 4px; }
.row.assistant .bubble { background: #fff; color: #1e293b; border: 1px solid #e2e8f0; border-bottom-left-radius: 4px; }
.bubble.processing { color: #94a3b8; font-style: italic; animation: pulse 1.2s ease-in-out infinite; }
.tool-line { font-family: ui-monospace, monospace; font-size: .8rem; color: #94a3b8; padding: .2rem .5rem; }
@keyframes pulse { 0%,100% { opacity: .45; } 50% { opacity: 1; } }
footer { display: flex; gap: .5rem; padding: .8rem 1rem; border-top: 1px solid #e2e8f0; background: #fff; }
input { flex: 1; padding: .6rem .8rem; border: 1px solid #cbd5e1; border-radius: 12px; outline: none; font-size: .95rem; }
input:focus { border-color: #4f46d5; }
button { padding: .6rem 1.2rem; border: 0; border-radius: 12px; background: #4f46d5; color: #fff; font-size: .95rem; cursor: pointer; }
button:hover:not(:disabled) { background: #4338ca; }
button:disabled { background: #cbd5e1; cursor: not-allowed; }
</style>
```

- [ ] **Step 3: Create `SessionSidebar.vue`**

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'

const { sessions, currentSessionId, createSession, selectSession, deleteSession } = useSessions()

function relTime(ts: number): string {
  if (!ts) return ''
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return '刚刚'
  if (s < 3600) return Math.floor(s / 60) + '分钟前'
  if (s < 86400) return Math.floor(s / 3600) + '小时前'
  return Math.floor(s / 86400) + '天前'
}
</script>

<template>
  <aside class="sidebar">
    <button class="new-btn" @click="createSession">+ 新对话</button>
    <ul class="sess-list">
      <li
        v-for="s in sessions"
        :key="s.session_id"
        :class="['sess-item', { active: s.session_id === currentSessionId }]"
        @click="selectSession(s.session_id)"
      >
        <span class="sess-title">{{ s.title || '(无标题)' }}</span>
        <span class="sess-time">{{ relTime(s.last_message_at) }}</span>
        <span class="sess-del" @click.stop="deleteSession(s.session_id)">✕</span>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar { width: 240px; flex: 0 0 240px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; padding: .6rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (max-width: 640px) { .sidebar { width: 100%; flex: 0 0 auto; max-height: 30%; } }
.new-btn { padding: .55rem; border: 0; border-radius: 10px; background: #4f46d5; color: #fff; cursor: pointer; font-size: .9rem; margin-bottom: .5rem; }
.new-btn:hover { background: #4338ca; }
.sess-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; }
.sess-item { display: flex; align-items: center; gap: .4rem; padding: .5rem .5rem; border-radius: 8px; cursor: pointer; font-size: .85rem; }
.sess-item:hover { background: #f1f5f9; }
.sess-item.active { background: #eef2ff; }
.sess-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1e293b; }
.sess-time { color: #94a3b8; font-size: .72rem; flex-shrink: 0; }
.sess-del { color: #cbd5e1; flex-shrink: 0; }
.sess-del:hover { color: #ef4444; }
</style>
```

- [ ] **Step 4: Rewrite `App.vue` as the layout shell**

Replace `web/src/App.vue`:

```vue
<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessions } from './composables/useSessions'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatPanel from './components/ChatPanel.vue'
import TodoPanel from './components/TodoPanel.vue'

const { init } = useSessions()

onMounted(() => { init() })
</script>

<template>
  <div class="app">
    <SessionSidebar />
    <ChatPanel />
    <TodoPanel />
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; }
.app { display: flex; height: 100%; max-width: 1280px; margin: 0 auto; }
@media (max-width: 640px) { .app { flex-direction: column; max-width: 100%; } }
</style>
```

- [ ] **Step 5: Build to verify no type/compile errors**

Run: `cd web && npm run build 2>&1 | tail -20`
Expected: build succeeds.

- [ ] **Step 6: Manual end-to-end verification**

Start both backends (each in its own terminal):
```
python -m twinkle.agentserver
python -m twinkle.gateway
```
and the frontend: `cd web && npm run dev`. Open http://localhost:5173, set `TWINKLE_LLM_API_KEY` in `.env` (else the chat turn fails at the model call — ws plumbing still works). Verify each path:
- **New session:** click "+ 新对话" → sidebar adds an empty row; sending a message → the row gets an auto-title.
- **Switch session:** click another row → chat area loads that session's history (user/assistant bubbles, tool lines).
- **Refresh:** reload the page → the same session's history is still shown.
- **Delete:** click ✕ on a row → it disappears; if it was current, a fresh session is created.
- **Restart survives:** stop the AgentServer, restart it, reload the page → the past sessions still list and load (cold-start hydration from `history.json`).

Expected: all paths work.

- [ ] **Step 7: Commit**

```bash
git add web/src/App.vue web/src/components/
git commit -m "web: SessionSidebar + ChatPanel + TodoPanel + App shell"
```

---

### Task 10: Docs

**Files:**
- Modify: `docs/architecture.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a "Session persistence & history RPCs" section to `docs/architecture.md`**

Append a new section noting: this is an intentional extension past the roadmap's "no persistence" stance; per-session layout `<SESSIONS_DIR>/<sid>/{metadata.json,history.json}`; `history.json` preserves full OpenAI-native fields for cold-start ReAct reconstruction; the four RPCs (`session.create`/`session.list`/`session.delete`/`history.get`) dispatch in `session_rpc.py` and yield single `e2a.result` frames mapped to the browser `result` event; `session_id` is browser-generated and sticky in `localStorage`. Cross-link `docs/superpowers/specs/2026-07-22-session-management-design.md` and `docs/superpowers/plans/2026-07-22-session-management.md`.

- [ ] **Step 2: Update `CLAUDE.md`**

In the `session_store.py` bullet under AgentServer internals, replace the "in-memory dict... No persistence yet; interface allows swapping in SQLite later" wording with: "disk-backed (in-memory cache + JSON files under `SESSIONS_DIR`); `append`/`create_session`/`delete_session` async, `get_messages`/`list_sessions`/`get_history` sync; cold-start hydrates ReAct context from `history.json`". Also mention `session_rpc.py` and the `e2a.result` frame → `result` event mapping in the message-formats section.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md CLAUDE.md
git commit -m "docs: session persistence + history RPC architecture notes"
```

---

## Self-Review (run after writing)

**1. Spec coverage:**
- Persistence JSON layout — Task 1 (config) + Task 2 (store). ✓
- `history.json` preserves OpenAI fields for cold-start — Task 2 test `test_cold_start_hydrates_full_history` + `test_append_preserves_tool_calls_for_react`. ✓
- Auto-title — Task 2 `test_first_user_message_auto_titles`. ✓
- `create/list/delete/get_history` — Tasks 2–3. ✓
- `agent_loop` await + request_id — Task 4. ✓
- `e2a.result` frame + `result` event + MessageHandler branch — Task 5. ✓
- RPC dispatch + `ws_handler(loop, store)` routing — Task 6. ✓
- webClient sticky + `request()` — Task 7. ✓
- `useSessions` composable — Task 8. ✓
- Sidebar + ChatPanel + TodoPanel + App shell — Task 9. ✓
- Error handling (failed result frame, corrupt JSONL skip, metadata fallback, Lock) — Task 2 (Lock + corrupt skip + fallback) + Task 6 (failed frame). ✓
- Config + gitignore — Task 1. ✓
- Docs — Task 10. ✓
- Broadcast leak NOT fixed — Global Constraints + out-of-scope note; no task touches `WebChannel.send`. ✓

**2. Placeholder scan:** none — every code step contains real code; every test step contains real test code; commands are exact.

**3. Type/signature consistency:**
- `SessionStore(sessions_dir)` used identically in conftest, test_session_store, test_agent_loop, server.py. ✓
- `async append(session_id, message, request_id=None, event_type=None)` — matches agent_loop call sites and session_rpc (which doesn't call append; only tests do). ✓
- `dispatch_session_rpc(envelope, store)` in both `session_rpc.py` and `test_session_rpc.py`. ✓
- `handles_session_rpc` aliased in server.py from `handles`. ✓
- `WebClient.getSessionId/setSessionId/getLastRequestId/request` — used in `useSessions` and (will be) in components. `getLastRequestId` defined in Task 7, consumed in Task 8. ✓
- `useSessions` return shape consumed by all three components in Task 9. ✓

No issues found.
