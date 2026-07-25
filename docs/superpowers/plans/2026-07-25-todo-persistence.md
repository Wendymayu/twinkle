# Todo Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `TodoStore` disk-backed (`<TODOS_DIR>/<sid>.json`, load→mutate→save per op) so the agent's todo list survives process restarts, mirroring jiuwenswarm's `TodoToolkit` mechanics.

**Architecture:** Rewrite `TodoStore` from in-memory `dict[sid, list]` to per-session flat JSON files in a new `TODOS_DIR` (parallel to `sessions/`). No in-memory cache; per-session `asyncio.Lock` serializes read-modify-write. A process-level singleton accessor `get_todo_store()` shares one instance (one lock set) between the todo tools and the `session.delete` RPC cleanup. `create` semantics change: refuse only when an in-progress list exists; allow replacing an all-completed list.

**Tech Stack:** Python stdlib (`json`, `pathlib`, `asyncio`, `dataclasses`); pytest with `asyncio.run()` + `tmp_path` (no pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-07-25-todo-persistence-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `twinkle/config.py` | Add `TODOS_DIR` (parallel to `SESSIONS_DIR`) | Modify |
| `twinkle/agentserver/todo/store.py` | Disk-backed `TodoStore` + `TodoTask` + `TodoError` | Rewrite |
| `twinkle/agentserver/todo/__init__.py` | Re-exports + `get_todo_store()`/`_set_todo_store()` singleton accessor | Modify |
| `twinkle/agentserver/tools/builtin/todo_tools.py` | 3 `@tool` fns call `get_todo_store()` at call time | Modify |
| `twinkle/agentserver/sessions/handlers.py` | `session.delete` RPC also deletes the todo file | Modify |
| `tests/conftest.py` | `todos_dir`/`todo_store`/`isolated_todo_store` fixtures | Modify |
| `tests/test_config_context.py` | Assert `TODOS_DIR` default | Modify |
| `tests/test_todo_store.py` | Disk-behavior tests (existing updated + new) | Rewrite |
| `tests/test_todo_tools.py` | Autouse tmp-store swap; fix `"already exists"`→`"in progress"` | Modify |
| `tests/test_agent_loop.py` | 2 todo tests: use `isolated_todo_store`; drop `_todo_store` import | Modify |
| `tests/test_session_rpc.py` | Existing delete test swaps store; new cleanup test | Modify |
| `tests/test_todo_accessor.py` | `get_todo_store`/`_set_todo_store` contract | Create |
| `docs/design/todo-design.md` | §存储策略 + §差异表: memory→disk | Modify |
| `CLAUDE.md` | `todo_store.py` entry + config table `TWINKLE_TODOS_DIR` row | Modify |
| `docs/architecture.md` | §4.x todo storage if present | Modify/verify |

`agent_loop.py`, `sessions/store.py`, `server.py`, `__main__.py`: **unchanged** (todo routing/event wiring and session RPC signatures stay; the singleton is accessed where needed, not threaded).

---

## Task 1: Add `TODOS_DIR` config

**Files:**
- Modify: `twinkle/config.py` (after the `SESSIONS_DIR` block, ~line 69)
- Test: `tests/test_config_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_context.py`:

```python
def test_todos_dir_default_present():
    from pathlib import Path
    assert isinstance(config.TODOS_DIR, str) and config.TODOS_DIR
    p = Path(config.TODOS_DIR)
    assert p.name == "todos"
    assert p.parent.name == ".twinkle_data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_context.py::test_todos_dir_default_present -v`
Expected: FAIL with `AttributeError: module 'twinkle.config' has no attribute 'TODOS_DIR'`

- [ ] **Step 3: Add `TODOS_DIR` to config**

In `twinkle/config.py`, insert this block immediately after the `SESSIONS_DIR = ...` assignment (after line 69):

```python
# --- Todos persistence (disk-backed per-session todo store) ---
# Flat layout: <TODOS_DIR>/<session_id>.json (one file per session). Defaults
# to <WORKSPACE_DIR>/.twinkle_data/todos — parallel to sessions/, NOT co-located
# inside the session dir, so session deletion must explicitly clean up todos
# (TodoStore.delete wired into the session.delete RPC). Override with
# TWINKLE_TODOS_DIR (~/... expanded).
TODOS_DIR = os.getenv("TWINKLE_TODOS_DIR") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "todos"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_context.py::test_todos_dir_default_present -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/config.py tests/test_config_context.py
git commit -m "config: add TODOS_DIR for disk-backed todo store"
```

---

## Task 2: Disk-backed `TodoStore` (core) + fixtures

Rewrite `TodoStore` to disk JSON, load→mutate→save per op, with the new `create` semantics. Drives the change via updated existing tests + new persistence/semantics/corrupt tests.

**Files:**
- Modify: `tests/conftest.py` (add `todos_dir` + `todo_store` fixtures)
- Rewrite: `twinkle/agentserver/todo/store.py`
- Rewrite: `tests/test_todo_store.py`

- [ ] **Step 1: Add `todos_dir` + `todo_store` fixtures to conftest**

Append to `tests/conftest.py` (after the `session_store` fixture):

```python
@pytest.fixture
def todos_dir(tmp_path) -> "Path":
    """A fresh per-test todos directory (disk-backed TodoStore target)."""
    d = tmp_path / "todos"
    d.mkdir()
    return d


@pytest.fixture
def todo_store(todos_dir):
    """A TodoStore rooted in a per-test tmp dir (no repo pollution)."""
    from twinkle.agentserver.todo.store import TodoStore
    return TodoStore(str(todos_dir))
```

- [ ] **Step 2: Rewrite `tests/test_todo_store.py` with fixture-based + new tests**

Replace the entire contents of `tests/test_todo_store.py`:

```python
# tests/test_todo_store.py
import asyncio
import json

import pytest

from twinkle.agentserver.todo import TodoError, TodoStore


def test_create_then_list(todo_store) -> None:
    async def run():
        await todo_store.create("s1", ["a", "b"])
        return await todo_store.list_tasks("s1")

    listed = asyncio.run(run())
    assert [t.idx for t in listed] == [1, 2]
    assert [t.title for t in listed] == ["a", "b"]
    assert all(t.status == "waiting" for t in listed)


def test_create_empty_raises(todo_store) -> None:
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(todo_store.create("s1", []))


def test_create_twice_refuses_while_in_progress(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="in progress"):
        asyncio.run(todo_store.create("s1", ["b"]))


def test_create_replaces_when_all_completed(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1))
    asyncio.run(todo_store.complete("s1", 2))
    # all completed -> create allowed, replaces the old list
    asyncio.run(todo_store.create("s1", ["c"]))
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert [t.title for t in listed] == ["c"]
    assert all(t.status == "waiting" for t in listed)


def test_complete_marks_status_and_result(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1, result="done A"))
    tasks = asyncio.run(todo_store.list_tasks("s1"))
    assert tasks[0].status == "completed"
    assert tasks[0].result == "done A"
    assert tasks[1].status == "waiting"


def test_complete_unknown_idx_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(todo_store.complete("s1", 99))


def test_complete_already_completed_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    asyncio.run(todo_store.complete("s1", 1))
    with pytest.raises(TodoError, match="already completed"):
        asyncio.run(todo_store.complete("s1", 1))


def test_sessions_isolated(todo_store) -> None:
    asyncio.run(todo_store.create("sA", ["a"]))
    asyncio.run(todo_store.create("sB", ["b"]))
    assert [t.title for t in asyncio.run(todo_store.list_tasks("sA"))] == ["a"]
    assert [t.title for t in asyncio.run(todo_store.list_tasks("sB"))] == ["b"]


def test_concurrent_complete_no_lost_update(todo_store) -> None:
    """Two coroutines completing different tasks on the same session
    must both succeed (no lost update from read-modify-write)."""
    asyncio.run(todo_store.create("s1", ["a", "b"]))

    async def run():
        await asyncio.gather(
            todo_store.complete("s1", 1, result="A"),
            todo_store.complete("s1", 2, result="B"),
        )
        return await todo_store.list_tasks("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}


def test_persistence_across_restart(todo_store, todos_dir) -> None:
    """A brand-new TodoStore pointing at the same dir sees the persisted list
    (no in-memory carryover across instances)."""
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    asyncio.run(todo_store.complete("s1", 1))

    cold = TodoStore(str(todos_dir))  # fresh instance, cold "cache"
    listed = asyncio.run(cold.list_tasks("s1"))
    assert [t.title for t in listed] == ["a", "b"]
    assert listed[0].status == "completed"
    assert listed[1].status == "waiting"


def test_load_corrupt_json_returns_empty(todo_store, todos_dir) -> None:
    (todos_dir / "s1.json").write_text("{not valid json", encoding="utf-8")
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert listed == []
    # create treats corrupt/missing as no list -> succeeds
    asyncio.run(todo_store.create("s1", ["fresh"]))
    listed = asyncio.run(todo_store.list_tasks("s1"))
    assert [t.title for t in listed] == ["fresh"]


def test_save_writes_json_with_full_fields(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    on_disk = json.loads((todos_dir / "s1.json").read_text(encoding="utf-8"))
    assert on_disk == [{"idx": 1, "title": "a", "status": "waiting", "result": ""}]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: FAIL — `TodoStore()` takes no args / `create` is in-memory (assertions on disk fail).

- [ ] **Step 4: Rewrite `twinkle/agentserver/todo/store.py`**

Replace the entire contents of `twinkle/agentserver/todo/store.py`:

```python
# twinkle/agentserver/todo/store.py
"""TodoStore — agent 内部任务规划的磁盘持久化存储。

per-session flat 文件 <todos_dir>/<session_id>.json,每次操作 load→改→save,
跨进程重启存活。对齐 jiuwenswarm TodoToolkit(todo_toolkits.py)的机制,沿用
Twinkle SessionStore 的 JSON/async 约定:
- 存 JSON(dataclass asdict 精确往返),不存 markdown;
- 无内存缓存,每次 op 都读写盘(数据极小,操作稀疏);
- per-session asyncio.Lock 串行化 read-modify-write;
- 文件缺失/损坏 → _load 返 [] 不抛(对齐 SessionStore 坏行跳过);
- 写盘 OSError → 包 TodoError,让工具层回错给模型,不炸 run_stream。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path

log = logging.getLogger("twinkle.agentserver.todo.store")


@dataclass
class TodoTask:
    idx: int
    title: str
    status: str  # "waiting" | "running" | "completed"
    result: str = ""


class TodoError(Exception):
    """业务级错误,消息可直接回给模型。"""


class TodoStore:
    def __init__(self, todos_dir: str | Path) -> None:
        self._root = Path(todos_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # --- paths & locks ---

    def _todo_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _lock(self, session_id: str) -> asyncio.Lock:
        # 单线程事件循环下 setdefault 无竞态(同步调用,无 await 间隙)。
        return self._locks.setdefault(session_id, asyncio.Lock())

    # --- I/O (load→mutate→save per op, no cache) ---

    def _load(self, session_id: str) -> list[TodoTask]:
        p = self._todo_path(session_id)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping corrupt todo file %s: %s", session_id, exc)
            return []
        if not isinstance(data, list):
            return []
        out: list[TodoTask] = []
        for rec in data:
            t = self._record_to_task(rec)
            if t is not None:
                out.append(t)
        return out

    def _save(self, session_id: str, tasks: list[TodoTask]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._todo_path(session_id).write_text(
                json.dumps(
                    [dataclasses.asdict(t) for t in tasks],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise TodoError(f"failed to persist todo: {exc}") from exc

    @staticmethod
    def _record_to_task(rec) -> "TodoTask | None":
        try:
            return TodoTask(
                idx=int(rec["idx"]),
                title=str(rec["title"]),
                status=str(rec["status"]),
                result=str(rec.get("result", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # --- public API ---

    async def create(self, session_id: str, tasks: list[str]) -> None:
        """Create a todo list for the session. Raises TodoError if tasks is
        empty, or if a list already exists with any non-completed task (guard
        against clobbering an in-progress plan). Replacing a list whose tasks
        are ALL completed is allowed (fresh plan after the previous finished).
        """
        if not tasks:
            raise TodoError("tasks must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._load(session_id)
            if existing and any(t.status != "completed" for t in existing):
                raise TodoError(
                    f"todo list already in progress for session {session_id}."
                )
            new = [
                TodoTask(idx=i + 1, title=t, status="waiting", result="")
                for i, t in enumerate(tasks)
            ]
            self._save(session_id, new)

    async def complete(
        self, session_id: str, idx: int, result: str = ""
    ) -> None:
        """Mark a task as completed. Raises TodoError if idx not found or
        the task is already completed."""
        async with self._lock(session_id):
            tasks = self._load(session_id)
            for t in tasks:
                if t.idx == idx:
                    if t.status == "completed":
                        raise TodoError(f"Task {idx} is already completed.")
                    t.status = "completed"
                    t.result = (result or "").strip() or "done"
                    self._save(session_id, tasks)
                    return
            raise TodoError(f"Task {idx} not found.")

    async def list_tasks(self, session_id: str) -> list[TodoTask]:
        async with self._lock(session_id):
            return list(self._load(session_id))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_todo_store.py twinkle/agentserver/todo/store.py
git commit -m "todo: disk-backed TodoStore (per-session json, load/save per op)"
```

---

## Task 3: `TodoStore.delete(sid)`

Add the cleanup method used by the `session.delete` RPC (Task 6).

**Files:**
- Modify: `twinkle/agentserver/todo/store.py` (append `delete` method)
- Test: `tests/test_todo_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_todo_store.py`:

```python
def test_delete_removes_file(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    p = todos_dir / "s1.json"
    assert p.is_file()
    assert asyncio.run(todo_store.delete("s1")) is True
    assert not p.exists()
    assert asyncio.run(todo_store.list_tasks("s1")) == []


def test_delete_missing_returns_false(todo_store) -> None:
    assert asyncio.run(todo_store.delete("never")) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_store.py::test_delete_removes_file tests/test_todo_store.py::test_delete_missing_returns_false -v`
Expected: FAIL with `AttributeError: 'TodoStore' object has no attribute 'delete'`

- [ ] **Step 3: Add `delete` method to `TodoStore`**

In `twinkle/agentserver/todo/store.py`, append this method to the `TodoStore` class (after `list_tasks`):

```python
    async def delete(self, session_id: str) -> bool:
        """Remove the session's todo file. Returns False if absent. Holds the
        per-session lock so a concurrent create/complete can't recreate the
        file mid-delete (orphan). Called by the session.delete RPC."""
        async with self._lock(session_id):
            p = self._todo_path(session_id)
            if not p.is_file():
                return False
            try:
                p.unlink()
            except OSError as exc:
                log.warning("todo delete failed for %s: %s", session_id, exc)
                return False
            return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/todo/store.py tests/test_todo_store.py
git commit -m "todo: add TodoStore.delete for session-delete cleanup"
```

---

## Task 4: `get_todo_store()` singleton accessor

Process-level singleton so the todo tools and the `session.delete` RPC share one instance (one lock set). Plus the test-swap hook and an `isolated_todo_store` fixture.

**Files:**
- Modify: `twinkle/agentserver/todo/__init__.py`
- Modify: `tests/conftest.py` (add `isolated_todo_store` fixture)
- Create: `tests/test_todo_accessor.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_todo_accessor.py`:

```python
# tests/test_todo_accessor.py
from twinkle.agentserver.todo import get_todo_store, _set_todo_store
from twinkle.agentserver.todo.store import TodoStore


def test_get_todo_store_returns_singleton(tmp_path):
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    try:
        a = get_todo_store()
        b = get_todo_store()
        assert a is b
    finally:
        _set_todo_store(None)


def test_set_todo_store_swaps(tmp_path):
    custom = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(custom)
    try:
        assert get_todo_store() is custom
    finally:
        _set_todo_store(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_accessor.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_todo_store'` (and `_set_todo_store`).

- [ ] **Step 3: Add accessor to `twinkle/agentserver/todo/__init__.py`**

Replace the entire contents of `twinkle/agentserver/todo/__init__.py`:

```python
"""todo 包入口 — re-exports + 进程级单例访问器。"""
from twinkle.agentserver.todo.store import TodoStore, TodoTask, TodoError
from twinkle.agentserver.todo.context import (
    PLAN_TODO_SESSION_ID, get_plan_todo_session_id,
    TODO_EVENTS, reset_todo_events, append_todo_event, flush_todo_events,
)


_TODO_STORE: TodoStore | None = None


def get_todo_store() -> TodoStore:
    """进程级单例 TodoStore(惰性构造,处处共享同一实例 + 同一套锁)。

    不像 sessions/__init__.py 的 session_store() 返 fresh 实例(DI 穿参用)——
    todo 工具是模块级 @tool 函数,不便接收 DI,故用单例访问器达到"一处构造、
    处处共享"。lazy import config 避免 import-time 副作用。
    """
    global _TODO_STORE
    if _TODO_STORE is None:
        from twinkle.config import TODOS_DIR
        _TODO_STORE = TodoStore(TODOS_DIR)
    return _TODO_STORE


def _set_todo_store(store: TodoStore | None) -> None:
    """测试钩子:替换/重置单例(配 tmp_path 盘)。生产代码不调。"""
    global _TODO_STORE
    _TODO_STORE = store


__all__ = [
    "TodoStore", "TodoTask", "TodoError",
    "PLAN_TODO_SESSION_ID", "get_plan_todo_session_id",
    "TODO_EVENTS", "reset_todo_events", "append_todo_event", "flush_todo_events",
    "get_todo_store", "_set_todo_store",
]
```

- [ ] **Step 4: Add `isolated_todo_store` fixture to conftest**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def isolated_todo_store(tmp_path):
    """Construct a tmp-backed TodoStore, set it as the process singleton (so
    get_todo_store() returns it during tests), yield it, and reset after.
    For tests that drive the todo tools or the agent loop's todo path."""
    from twinkle.agentserver.todo import _set_todo_store
    from twinkle.agentserver.todo.store import TodoStore
    s = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(s)
    yield s
    _set_todo_store(None)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_accessor.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/todo/__init__.py tests/conftest.py tests/test_todo_accessor.py
git commit -m "todo: add get_todo_store singleton accessor + test hook"
```

---

## Task 5: Wire todo tools to `get_todo_store()` + fix tests

Replace the `_todo_store` module singleton in `todo_tools.py` with call-time `get_todo_store()`. Update `test_todo_tools.py` (autouse tmp-store swap; fix the `"already exists"`→`"in progress"` assertion) and the two `test_agent_loop.py` todo tests (drop the `_todo_store` import; use `isolated_todo_store`).

**Files:**
- Rewrite: `twinkle/agentserver/tools/builtin/todo_tools.py`
- Modify: `tests/test_todo_tools.py`
- Modify: `tests/test_agent_loop.py` (2 tests)

- [ ] **Step 1: Add autouse tmp-store fixture + fix message assertion in `test_todo_tools.py`**

In `tests/test_todo_tools.py`, after the imports at the top, add:

```python
import pytest
from twinkle.agentserver.todo import _set_todo_store
from twinkle.agentserver.todo.store import TodoStore


@pytest.fixture(autouse=True)
def _isolated_todo_store(tmp_path):
    """Each test gets a tmp-backed todo singleton so the tools' get_todo_store()
    never writes to the real ~/.twinkle."""
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    yield
    _set_todo_store(None)
```

Then in `test_create_twice_returns_error_with_current_list`, change:

```python
    assert "already exists" in out
```
to:
```python
    assert "in progress" in out
```

- [ ] **Step 2: Run `test_todo_tools.py` to verify the message assertion fails (before wiring)**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: `test_create_twice_returns_error_with_current_list` **FAILS** — it now asserts `"in progress"` but the old `create` still raises `"already exists"`; all other tests **PASS** (the tools still use the in-memory module `_todo_store`, so the autouse singleton swap is unused by them and behavior is unchanged). This red state is the TDD driver for the tools rewrite in Step 3.

- [ ] **Step 3: Rewrite `twinkle/agentserver/tools/builtin/todo_tools.py`**

Replace the entire contents of `twinkle/agentserver/tools/builtin/todo_tools.py`:

```python
# twinkle/agentserver/tools/builtin/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

3 个 @tool:create / complete / list。读 plan_todo_context 拿当前 session_id,
经 get_todo_store() 取共享 TodoStore 单例操作,返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。TodoStore 的 create/complete 是纯写(返回 None),
工具层在 mutation 后调用 list_tasks() 取全量再拼 markdown + snapshot。业务错误
catch 成 "Error: ..." 字符串返回。

对齐 jiuwenclaw tools/todo_toolkits.py,砍 start/insert/remove/batch
与 op-result 总线。store 单例经 todo.get_todo_store() 进程级共享(工具 + session.delete
清理同一实例,同一套锁)。
"""
from __future__ import annotations

from twinkle.agentserver.todo import (
    get_plan_todo_session_id,
    get_todo_store,
    append_todo_event,
    TodoError, TodoTask,
)
from twinkle.agentserver.tools.decorator import tool

_ICON = {"waiting": "[ ]", "running": "[>]", "completed": "[x]"}


def _format_tasks(tasks: list[TodoTask]) -> str:
    if not tasks:
        return "No todo tasks."
    lines = []
    for t in tasks:
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        lines.append(f"- {icon} {t.idx}. {t.title}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks: list[TodoTask]) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


def _snapshot(tasks: list[TodoTask]) -> dict:
    """Structured todo snapshot for the UI (publish side-channel)."""
    waiting_running = sum(1 for t in tasks if t.status in ("waiting", "running"))
    completed = sum(1 for t in tasks if t.status == "completed")
    return {
        "tasks": [
            {"idx": t.idx, "title": t.title, "status": t.status, "result": t.result}
            for t in tasks
        ],
        "remaining": waiting_running,
        "total": waiting_running + completed,
    }


@tool
async def todo_create(tasks: list[str]) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task descriptions; fails if a todo list already exists for this session.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        await store.create(session_id, tasks)
        current = await store.list_tasks(session_id)
        append_todo_event(_snapshot(current))
        return _append_list(f"Created {len(current)} todo tasks.", current)
    except TodoError as exc:
        current = await store.list_tasks(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_complete(idx: int, result: str = "") -> str:
    """Mark a todo task as completed and save a brief result. Pass the 1-based idx and an optional short result string.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        await store.complete(session_id, idx, result)
        current = await store.list_tasks(session_id)
        append_todo_event(_snapshot(current))
        return _append_list(f"Task {idx} marked as completed.", current)
    except TodoError as exc:
        current = await store.list_tasks(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list() -> str:
    """List all current todo tasks with their status. Returns 'No todo tasks.' when empty.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    tasks = await store.list_tasks(session_id)
    return _format_tasks(tasks)
```

- [ ] **Step 4: Run `test_todo_tools.py` to verify it passes**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: PASS (the autouse swap now matches the tools' `get_todo_store()`; `"in progress"` assertion matches the new `create` message).

- [ ] **Step 5: Update the two `test_agent_loop.py` todo tests**

In `tests/test_agent_loop.py`:

(a) Change the signature of `test_todo_create_round_trip_through_loop` from:
```python
def test_todo_create_round_trip_through_loop(session_store) -> None:
```
to:
```python
def test_todo_create_round_trip_through_loop(session_store, isolated_todo_store) -> None:
```
And replace lines 201-203 (the `from ... import _todo_store` block + two asserts):
```python
    from twinkle.agentserver.tools.builtin.todo_tools import _todo_store
    assert len(asyncio.run(_todo_store.list_tasks("s-todo"))) == 2
    assert asyncio.run(_todo_store.list_tasks("default")) == []
```
with:
```python
    # ContextVar was set to the envelope's session_id; the loop's todo_create
    # wrote to the shared singleton (= isolated_todo_store).
    assert len(asyncio.run(isolated_todo_store.list_tasks("s-todo"))) == 2
    assert asyncio.run(isolated_todo_store.list_tasks("default")) == []
```

(b) Change the signature of `test_todo_update_frame_emitted_on_create` from:
```python
def test_todo_update_frame_emitted_on_create(session_store) -> None:
```
to:
```python
def test_todo_update_frame_emitted_on_create(session_store, isolated_todo_store) -> None:
```
(No body change — the `isolated_todo_store` fixture sets the singleton so the loop's `todo_create` writes to tmp, not real `~/.twinkle`.)

- [ ] **Step 6: Run the agent-loop todo tests to verify they pass**

Run: `python -m pytest tests/test_agent_loop.py::test_todo_create_round_trip_through_loop tests/test_agent_loop.py::test_todo_update_frame_emitted_on_create -v`
Expected: PASS

- [ ] **Step 7: Run the full todo + agent-loop suites to confirm no regressions**

Run: `python -m pytest tests/test_todo_store.py tests/test_todo_tools.py tests/test_todo_accessor.py tests/test_agent_loop.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add twinkle/agentserver/tools/builtin/todo_tools.py tests/test_todo_tools.py tests/test_agent_loop.py
git commit -m "todo: wire tools to get_todo_store singleton; update tests"
```

---

## Task 6: `session.delete` RPC cleans todo

Wire `get_todo_store().delete(sid)` into the `session.delete` RPC so an orphan `<sid>.json` doesn't outlive its session.

**Files:**
- Modify: `twinkle/agentserver/sessions/handlers.py`
- Modify: `tests/test_session_rpc.py`

- [ ] **Step 1: Write the failing test + update the existing delete test**

In `tests/test_session_rpc.py`:

(a) Update the existing `test_session_delete_removes_and_returns_result` signature from:
```python
def test_session_delete_removes_and_returns_result(session_store, sessions_dir):
```
to:
```python
def test_session_delete_removes_and_returns_result(session_store, sessions_dir, isolated_todo_store):
```
(No body change — the fixture ensures `session.delete`'s `get_todo_store()` call doesn't pollute `~/.twinkle`.)

(b) Append a new test:

```python
def test_session_delete_cleans_todo(session_store, isolated_todo_store):
    _run(session_store.create_session("s1"))
    _run(isolated_todo_store.create("s1", ["a", "b"]))
    assert _run(isolated_todo_store.list_tasks("s1"))  # has tasks

    frames = _run(_frames(_env("session.delete", session_id="s1"), session_store))
    f = frames[0]
    assert f.body["type"] == "session.delete"
    # todo file cleaned up by the RPC -> list returns []
    assert _run(isolated_todo_store.list_tasks("s1")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_rpc.py::test_session_delete_cleans_todo -v`
Expected: FAIL — `list_tasks("s1")` still returns the 2 tasks (the RPC doesn't delete the todo yet).

- [ ] **Step 3: Wire cleanup into the `session.delete` RPC**

In `twinkle/agentserver/sessions/handlers.py`, add the import at the top (after the `from twinkle.agentserver.sessions.store import SessionStore` line):

```python
from twinkle.agentserver.todo import get_todo_store
```

Then change the `session.delete` branch from:
```python
        elif method == "session.delete":
            await store.delete_session(sid)
            body = {"type": "session.delete", "session_id": sid}
```
to:
```python
        elif method == "session.delete":
            await store.delete_session(sid)
            await get_todo_store().delete(sid)
            body = {"type": "session.delete", "session_id": sid}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_rpc.py -v`
Expected: PASS (including the new cleanup test).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/sessions/handlers.py tests/test_session_rpc.py
git commit -m "todo: session.delete RPC cleans up the session's todo file"
```

---

## Task 7: Docs sync

Update the design doc, CLAUDE.md, and architecture.md to reflect disk persistence.

**Files:**
- Modify: `docs/design/todo-design.md`
- Modify: `CLAUDE.md`
- Verify/Modify: `docs/architecture.md`

- [ ] **Step 1: Update `docs/design/todo-design.md` §存储策略**

In `docs/design/todo-design.md`, replace the "## 存储策略" section (the block starting with `**纯内存**，不持久化。`) with:

```markdown
## 存储策略

**磁盘持久化**，per-session flat 文件 `<TODOS_DIR>/<session_id>.json`，每次操作 load→改→save，跨进程重启存活。与 SessionStore 的"磁盘 + 缓存"策略不同（todo 无内存缓存——数据极小、操作稀疏，缓存收益微乎其微），但同属 disk-backed。

- `TODOS_DIR` 默认 `<WORKSPACE>/.twinkle_data/todos`，与 `sessions/` 并列（不 co-locate 进 session 目录），故 `session.delete` RPC 显式调 `TodoStore.delete(sid)` 清孤儿文件。
- 格式 JSON（`dataclass asdict` 精确往返），不存 markdown（与 jiuwenclaw 的 todo.md 不同——避免解析器/bug 面）。
- `TodoStore` 用 per-session `asyncio.Lock` 串行化 read-modify-write；进程级单例 `get_todo_store()` 让工具与 `session.delete` 清理共享同一实例/同一套锁。
- 文件缺失/损坏 → `_load` 返 `[]` 不抛（对齐 SessionStore 坏行跳过）；写盘 `OSError` → 包 `TodoError` 回给模型，不炸 `run_stream`。

### `create` 语义

持久化后旧列表跨重启存活，`create` 不再"已有即拒绝"：旧列表**有未完成任务**时拒绝（防误覆盖进行中规划）；**全部完成**时允许 `create` 替换（长会话内可多次规划）；无列表则创建。过往规划已在 `history.json`（tool 消息）留底，结构化 todo 只持"当前这条"。
```

- [ ] **Step 2: Update `docs/design/todo-design.md` §与 jiuwenclaw 的差异 table**

In the same file, in the "## 与 jiuwenclaw 的差异" table, change the 存储 row:

| | jiuwenclaw | Twinkle |
|---|---|---|
| 存储 | todo.md 文件 | 纯内存 dict |

to:

| | jiuwenclaw | Twinkle |
|---|---|---|
| 存储 | todo.md 文件 | 磁盘 JSON（`<TODOS_DIR>/<sid>.json`，独立目录，`session.delete` 显式清理） |

- [ ] **Step 3: Update `CLAUDE.md` `todo_store.py` entry**

In `CLAUDE.md`, in the AgentServer internals section, change the `todo_store.py` bullet from:

```
- **`todo_store.py`** — in-memory `TodoStore` (`dict[session_id, list[TodoTask]]` + per-session `asyncio.Lock` serializing read-modify-write). Methods: `create`/`complete`/`list_tasks`. No persistence (matches SessionStore philosophy).
```

to:

```
- **`todo_store.py`** — disk-backed `TodoStore` (`<TODOS_DIR>/<session_id>.json`, load→mutate→save per op, per-session `asyncio.Lock` serializing read-modify-write). Methods: `create`/`complete`/`list_tasks`/`delete`. Survives restart; `delete(sid)` called by the `session.delete` RPC. `create` refuses only when an in-progress list exists (replacing an all-completed list is allowed). Shared process singleton via `todo/__init__.py::get_todo_store()`.
```

- [ ] **Step 4: Add `TWINKLE_TODOS_DIR` row to `CLAUDE.md` config table**

In `CLAUDE.md`'s Configuration table, after the `TWINKLE_LOG_DIR` row, add:

```
| `TWINKLE_TODOS_DIR` | `<WORKSPACE>/.twinkle_data/todos` | Todo persistence root; flat `<sid>.json` per session, parallel to `sessions/`; `session.delete` cleans up via `TodoStore.delete` |
```

- [ ] **Step 5: Check `docs/architecture.md` for todo storage mentions**

Run: `python -m pytest tests/ -q` (sanity — docs change shouldn't break tests, but confirm the suite is still green).

Then grep `docs/architecture.md` for "todo": if §4.x describes todo storage as in-memory, update it to disk-backed (mirror the `todo_store.py` entry wording). If it only mentions todo routing/events (not storage), no change needed.

- [ ] **Step 6: Commit**

```bash
git add docs/design/todo-design.md CLAUDE.md docs/architecture.md
git commit -m "docs: sync todo storage to disk-backed persistence"
```

---

## Final Verification

- [ ] **Full test suite green**

Run: `python -m pytest tests/ -v`
Expected: all PASS (no regressions; no `~/.twinkle` pollution — every `get_todo_store()` caller swaps to tmp via a fixture).

- [ ] **Manual persistence check (optional, if LLM key available)**

Start both backends (`python scripts/start_services.py`), create a multi-step task via the web UI (agent calls `todo_create`), confirm the TodoPanel shows tasks. Restart the agentserver process. Reload the session — `todo_list` (or a new turn) should still see the persisted list (or, if all-completed, allow a fresh `create`). Confirm `<TODOS_DIR>/<sid>.json` exists on disk.

---

## Self-Review (run before handoff)

**1. Spec coverage** — each spec requirement → task:
- D1 single-list model → Task 2 (`create` replace-on-completed) ✓
- D2 `create` semantics (in-progress refuse / all-completed replace) → Task 2 tests ✓
- D3 JSON + no-cache + `TODOS_DIR` + per-session lock → Tasks 1, 2 ✓
- D4 `session.delete` cleanup + shared singleton → Tasks 4, 6 ✓
- D5 corrupt→[] / write-OSError→TodoError → Task 2 (`_load`/`_save`) + `test_load_corrupt_json_returns_empty` ✓
- Testing § (persistence/replace/refuse/corrupt/delete) → Tasks 2, 3 ✓
- Docs sync → Task 7 ✓
- `agent_loop.py` unchanged → confirmed (no task touches it) ✓

**2. Placeholder scan** — no TBD/TODO/vague; every code step shows full code; every run step has exact command + expected result. ✓

**3. Type/name consistency** — `TodoStore(todos_dir)`, `_todo_path`, `_load`/`_save`/`_record_to_task`, `create`/`complete`/`list_tasks`/`delete`, `get_todo_store`/`_set_todo_store`, `isolated_todo_store` fixture — same names across all tasks. `create` error message `"already in progress"` consistent in store.py, test_todo_store (`match="in progress"`), test_todo_tools (`assert "in progress" in out`). ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-25-todo-persistence.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session via executing-plans, batch with checkpoints.

Which approach?
