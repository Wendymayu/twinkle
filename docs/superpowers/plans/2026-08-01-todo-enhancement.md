# Phase 8 — Todo 增强实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Todo 系统从扁平清单增强为结构化任务追踪（id、blocked_by、sequential、owner、metadata）。

**Architecture:** 增强现有 TodoStore 数据模型和 API，重写 4 个工具（todo_create/update/list/get），更新 agent loop system prompt 和事件 snapshot，更新前端 TypeScript 类型和 TodoPanel 渲染。不新增独立系统，不改存储引擎，不改 WebSocket 事件类型。

**Tech Stack:** Python 3.11+ (dataclass, uuid, asyncio), Vue 3 + TypeScript

## Global Constraints

- 不做向后兼容——旧格式 .json 直接丢弃（spec §9）
- 工具名保持 `todo_*`，不改名（spec §3）
- WebSocket 事件类型保持 `e2a.todo_update` / `todo.update`（spec §7.2）
- 不加 `active_form` 字段（spec §4.3）
- `todo_create` 不暴露 `blocked_by` 参数；`todo_update` 不暴露 `blocked_by` 参数（spec §9）
- 测试用 `asyncio.run()` + `free_port`/`port_factory` fixtures，不用 `pytest-asyncio`（CLAUDE.md）

---

## File Structure

| File | Responsibility |
|---|---|
| `twinkle/agentserver/todo/store.py` | `TodoTask` dataclass + `TodoStore` CRUD (create/update/list/get/delete) |
| `twinkle/agentserver/todo/context.py` | ContextVar (PLAN_TODO_SESSION_ID, TODO_EVENTS) + snapshot helper |
| `twinkle/agentserver/todo/__init__.py` | Re-exports + `get_todo_store()` singleton |
| `twinkle/agentserver/tools/builtin/todo_tools.py` | 4 @tool functions: todo_create, todo_update, todo_list, todo_get |
| `twinkle/agentserver/tools/__init__.py` | Tool registration (replace todo_complete with todo_update + todo_get) |
| `twinkle/agentserver/agent_loop.py` | System prompt update (§7.1) |
| `web/src/services/webClient.ts` | TodoTask TypeScript interface |
| `web/src/composables/useSessions.ts` | TodoState type + box() + completedCount |
| `web/src/components/TodoPanel.vue` | Status-grouped rendering + blocked_by + owner display |
| `tests/test_todo_store.py` | TodoStore unit tests (full rewrite) |
| `tests/test_todo_tools.py` | Tool integration tests (full rewrite) |

---

### Task 1: TodoTask 数据模型 + TodoStore API 重写

**Files:**
- Modify: `twinkle/agentserver/todo/store.py` (full rewrite)
- Test: `tests/test_todo_store.py` (full rewrite)

**Interfaces:**
- Produces: `TodoTask` dataclass with fields `id, subject, description, status, result, blocked_by, owner, metadata, created_at, updated_at`; `TodoStore` methods `create(session_id, subjects, sequential) -> list[TodoTask]`, `update(session_id, task_id, **fields) -> tuple[TodoTask, str | None]`, `list(session_id, status=None) -> list[TodoTask]`, `get(session_id, task_id) -> TodoTask | None`, `delete(session_id) -> bool`; `TodoError` exception

- [ ] **Step 1: Write failing tests for new TodoTask dataclass**

```python
# tests/test_todo_store.py
import asyncio
import json
import time

import pytest

from twinkle.agentserver.todo import TodoError, TodoStore
from twinkle.agentserver.todo.store import TodoTask


def test_task_dataclass_fields() -> None:
    t = TodoTask(id="abc", subject="hello")
    assert t.id == "abc"
    assert t.subject == "hello"
    assert t.description == ""
    assert t.status == "pending"
    assert t.result == ""
    assert t.blocked_by == []
    assert t.owner == ""
    assert t.metadata == {}
    assert t.created_at == 0.0
    assert t.updated_at == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_todo_store.py::test_task_dataclass_fields -v`
Expected: FAIL — `TodoTask` still has old fields

- [ ] **Step 3: Rewrite TodoTask dataclass and TodoStore**

Replace the entire `twinkle/agentserver/todo/store.py` with:

```python
# twinkle/agentserver/todo/store.py
"""TodoStore — agent 内部任务规划的磁盘持久化存储。

per-session flat 文件 <todos_dir>/<session_id>.json, 每次操作 load→改→save,
跨进程重启存活。TodoTask 数据模型增强：id(UUID)、subject、description、
blocked_by、owner、metadata、created_at/updated_at。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger("twinkle.agentserver.todo.store")

_VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


@dataclasses.dataclass
class TodoTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    result: str = ""
    blocked_by: list[str] = dataclasses.field(default_factory=list)
    owner: str = ""
    metadata: dict = dataclasses.field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


class TodoError(Exception):
    """业务级错误, 消息可直接回给模型。"""


class TodoStore:
    def __init__(self, todos_dir: str | Path) -> None:
        self._root = Path(todos_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # --- paths & locks ---

    def _todo_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # --- I/O ---

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
    def _record_to_task(rec: dict) -> "TodoTask | None":
        try:
            return TodoTask(
                id=str(rec["id"]),
                subject=str(rec["subject"]),
                description=str(rec.get("description", "")),
                status=str(rec.get("status", "pending")),
                result=str(rec.get("result", "")),
                blocked_by=list(rec.get("blocked_by", [])),
                owner=str(rec.get("owner", "")),
                metadata=dict(rec.get("metadata", {})),
                created_at=float(rec.get("created_at", 0.0)),
                updated_at=float(rec.get("updated_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _find_by_id(tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        for t in tasks:
            if t.id == task_id:
                return t
        return None

    # --- public API ---

    async def create(
        self,
        session_id: str,
        subjects: list[str],
        sequential: bool = False,
    ) -> list[TodoTask]:
        """Create tasks for the session. Raises TodoError if subjects is empty,
        or if tasks already exist for this session (guard against clobbering)."""
        if not subjects:
            raise TodoError("subjects must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._load(session_id)
            if existing:
                raise TodoError(
                    f"todo list already exists for session {session_id}."
                )
            now = time.time()
            tasks = [
                TodoTask(
                    id=str(uuid.uuid4()),
                    subject=s,
                    created_at=now,
                    updated_at=now,
                )
                for s in subjects
            ]
            if sequential:
                for i, t in enumerate(tasks):
                    if i > 0:
                        t.blocked_by = [tasks[i - 1].id]
            self._save(session_id, tasks)
            return tasks

    async def update(
        self,
        session_id: str,
        task_id: str,
        *,
        status: str | None = None,
        result: str | None = None,
        owner: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[TodoTask, str | None]:
        """Update a task. Returns (task, warning) where warning is None on
        success or a string when blocked_by dependencies are unresolved.
        Raises TodoError if task_id not found."""
        async with self._lock(session_id):
            tasks = self._load(session_id)
            task = self._find_by_id(tasks, task_id)
            if task is None:
                raise TodoError(f"Task {task_id} not found.")
            now = time.time()
            warning = None
            if status is not None:
                if status not in _VALID_STATUSES:
                    raise TodoError(
                        f"Invalid status '{status}'. Must be one of {sorted(_VALID_STATUSES)}."
                    )
                # Guard: check blocked_by when transitioning to in_progress
                if status == "in_progress" and task.blocked_by:
                    unresolved = [
                        bid
                        for bid in task.blocked_by
                        if self._find_by_id(tasks, bid) is None
                        or self._find_by_id(tasks, bid).status != "completed"
                    ]
                    if unresolved:
                        warning = (
                            f"Warning: task {task_id} has unresolved "
                            f"dependencies: {unresolved}"
                        )
                task.status = status
            if result is not None:
                task.result = (result or "").strip() or "done"
            if owner is not None:
                task.owner = owner
            if metadata is not None:
                # merge-style: update keys, delete keys with None value
                for k, v in metadata.items():
                    if v is None:
                        task.metadata.pop(k, None)
                    else:
                        task.metadata[k] = v
            task.updated_at = now
            self._save(session_id, tasks)
            return task, warning

    async def list(
        self,
        session_id: str,
        status: str | None = None,
    ) -> list[TodoTask]:
        async with self._lock(session_id):
            tasks = self._load(session_id)
            if status is not None:
                tasks = [t for t in tasks if t.status == status]
            return tasks

    async def get(
        self,
        session_id: str,
        task_id: str,
    ) -> TodoTask | None:
        async with self._lock(session_id):
            return self._find_by_id(self._load(session_id), task_id)

    async def delete(self, session_id: str) -> bool:
        """Remove the session's todo file. Returns False if absent."""
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

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_todo_store.py::test_task_dataclass_fields -v`
Expected: PASS

- [ ] **Step 5: Write full TodoStore test suite**

Replace `tests/test_todo_store.py` with:

```python
# tests/test_todo_store.py
import asyncio
import json

import pytest

from twinkle.agentserver.todo import TodoError, TodoStore
from twinkle.agentserver.todo.store import TodoTask


def test_task_dataclass_fields() -> None:
    t = TodoTask(id="abc", subject="hello")
    assert t.id == "abc"
    assert t.subject == "hello"
    assert t.description == ""
    assert t.status == "pending"
    assert t.result == ""
    assert t.blocked_by == []
    assert t.owner == ""
    assert t.metadata == {}
    assert t.created_at == 0.0
    assert t.updated_at == 0.0


def test_create_then_list(todo_store) -> None:
    async def run():
        await todo_store.create("s1", ["a", "b"])
        return await todo_store.list("s1")

    listed = asyncio.run(run())
    assert len(listed) == 2
    assert [t.subject for t in listed] == ["a", "b"]
    assert all(t.status == "pending" for t in listed)
    assert all(t.id for t in listed)  # non-empty UUIDs


def test_create_returns_tasks_with_ids(todo_store) -> None:
    async def run():
        return await todo_store.create("s1", ["alpha", "beta"])

    tasks = asyncio.run(run())
    assert len(tasks) == 2
    assert tasks[0].subject == "alpha"
    assert tasks[0].id  # UUID assigned
    assert tasks[0].created_at > 0


def test_create_empty_raises(todo_store) -> None:
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(todo_store.create("s1", []))


def test_create_twice_refuses_while_in_progress(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="already exists"):
        asyncio.run(todo_store.create("s1", ["b"]))


def test_sequential_sets_blocked_by(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b", "c"], sequential=True)
        return tasks

    tasks = asyncio.run(run())
    assert tasks[0].blocked_by == []
    assert tasks[1].blocked_by == [tasks[0].id]
    assert tasks[2].blocked_by == [tasks[1].id]


def test_sequential_false_no_blocked_by(todo_store) -> None:
    async def run():
        return await todo_store.create("s1", ["a", "b"])

    tasks = asyncio.run(run())
    assert all(t.blocked_by == [] for t in tasks)


def test_update_status(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, warning = await todo_store.update("s1", tasks[0].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is None


def test_update_completed(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, status="completed", result="done A")
        return task

    task = asyncio.run(run())
    assert task.status == "completed"
    assert task.result == "done A"


def test_update_owner_and_metadata(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, owner="agent-1", metadata={"key": "val"})
        return task

    task = asyncio.run(run())
    assert task.owner == "agent-1"
    assert task.metadata == {"key": "val"}


def test_update_metadata_merge_style(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        await todo_store.update("s1", tasks[0].id, metadata={"k1": "v1", "k2": "v2"})
        task, _ = await todo_store.update("s1", tasks[0].id, metadata={"k2": None, "k3": "v3"})
        return task

    task = asyncio.run(run())
    assert task.metadata == {"k1": "v1", "k3": "v3"}


def test_update_blocked_by_warning(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"], sequential=True)
        # b is blocked by a; marking b as in_progress should warn
        task, warning = await todo_store.update("s1", tasks[1].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is not None
    assert "unresolved" in warning


def test_update_no_warning_when_blocked_by_completed(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"], sequential=True)
        await todo_store.update("s1", tasks[0].id, status="completed")
        task, warning = await todo_store.update("s1", tasks[1].id, status="in_progress")
        return task, warning

    task, warning = asyncio.run(run())
    assert task.status == "in_progress"
    assert warning is None


def test_update_unknown_task_raises(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(todo_store.update("s1", "nonexistent-id", status="completed"))


def test_update_invalid_status_raises(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        await todo_store.update("s1", tasks[0].id, status="invalid")

    with pytest.raises(TodoError, match="Invalid status"):
        asyncio.run(run())


def test_get_found(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        return await todo_store.get("s1", tasks[0].id)

    task = asyncio.run(run())
    assert task is not None
    assert task.subject == "a"


def test_get_not_found(todo_store) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    assert asyncio.run(todo_store.get("s1", "nonexistent")) is None


def test_list_with_status_filter(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b", "c"])
        await todo_store.update("s1", tasks[0].id, status="completed")
        await todo_store.update("s1", tasks[1].id, status="in_progress")
        return await todo_store.list("s1", status="pending")

    pending = asyncio.run(run())
    assert len(pending) == 1
    assert pending[0].subject == "c"


def test_sessions_isolated(todo_store) -> None:
    asyncio.run(todo_store.create("sA", ["a"]))
    asyncio.run(todo_store.create("sB", ["b"]))
    assert [t.subject for t in asyncio.run(todo_store.list("sA"))] == ["a"]
    assert [t.subject for t in asyncio.run(todo_store.list("sB"))] == ["b"]


def test_concurrent_update_no_lost_update(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a", "b"])
        await asyncio.gather(
            todo_store.update("s1", tasks[0].id, status="completed", result="A"),
            todo_store.update("s1", tasks[1].id, status="completed", result="B"),
        )
        return await todo_store.list("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}


def test_persistence_across_restart(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a", "b"]))
    tasks = asyncio.run(todo_store.list("s1"))
    asyncio.run(todo_store.update("s1", tasks[0].id, status="completed"))

    cold = TodoStore(str(todos_dir))
    listed = asyncio.run(cold.list("s1"))
    assert [t.subject for t in listed] == ["a", "b"]
    assert listed[0].status == "completed"
    assert listed[1].status == "pending"


def test_load_corrupt_json_returns_empty(todo_store, todos_dir) -> None:
    (todos_dir / "s1.json").write_text("{not valid json", encoding="utf-8")
    listed = asyncio.run(todo_store.list("s1"))
    assert listed == []
    asyncio.run(todo_store.create("s1", ["fresh"]))
    listed = asyncio.run(todo_store.list("s1"))
    assert [t.subject for t in listed] == ["fresh"]


def test_save_writes_json_with_new_fields(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    on_disk = json.loads((todos_dir / "s1.json").read_text(encoding="utf-8"))
    task = on_disk[0]
    assert "id" in task
    assert task["subject"] == "a"
    assert task["status"] == "pending"
    assert "blocked_by" in task
    assert "owner" in task
    assert "metadata" in task
    assert "created_at" in task
    assert "updated_at" in task


def test_delete_removes_file(todo_store, todos_dir) -> None:
    asyncio.run(todo_store.create("s1", ["a"]))
    p = todos_dir / "s1.json"
    assert p.is_file()
    assert asyncio.run(todo_store.delete("s1")) is True
    assert not p.exists()
    assert asyncio.run(todo_store.list("s1")) == []


def test_delete_missing_returns_false(todo_store) -> None:
    assert asyncio.run(todo_store.delete("never")) is False


def test_cancelled_status(todo_store) -> None:
    async def run():
        tasks = await todo_store.create("s1", ["a"])
        task, _ = await todo_store.update("s1", tasks[0].id, status="cancelled")
        return task

    task = asyncio.run(run())
    assert task.status == "cancelled"
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/todo/store.py tests/test_todo_store.py
git commit -m "feat: enhance TodoTask data model + rewrite TodoStore API"
```

---

### Task 2: 更新 todo 包导出 + context snapshot

**Files:**
- Modify: `twinkle/agentserver/todo/__init__.py`
- Modify: `twinkle/agentserver/todo/context.py` (no changes needed — context.py is fine as-is)

**Interfaces:**
- Consumes: `TodoTask` from Task 1 (new fields)
- Produces: `get_todo_store()` returns enhanced `TodoStore`; `_snapshot()` helper will be in todo_tools.py (Task 3)

- [ ] **Step 1: Update `__init__.py` re-exports**

The `__init__.py` already re-exports `TodoStore`, `TodoTask`, `TodoError` from `store.py` and all context vars from `context.py`. No changes needed to the re-exports — they automatically pick up the new `TodoTask` fields.

Verify by running:

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: All PASS (confirms imports still work)

- [ ] **Step 2: Commit**

No code changes in this task — the re-exports already work. Skip commit; move to Task 3.

---

### Task 3: 重写 4 个 todo 工具

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/todo_tools.py` (full rewrite)
- Modify: `twinkle/agentserver/tools/__init__.py` (replace todo_complete with todo_update + todo_get)
- Test: `tests/test_todo_tools.py` (full rewrite)

**Interfaces:**
- Consumes: `TodoStore` from Task 1 (`create`, `update`, `list`, `get`); `get_plan_todo_session_id()`, `append_todo_event()` from context.py
- Produces: 4 `@tool` functions: `todo_create(subjects, sequential)`, `todo_update(task_id, status, result, owner, metadata)`, `todo_list(status)`, `todo_get(task_id)`; `_snapshot()` helper that produces the extended event payload

- [ ] **Step 1: Rewrite todo_tools.py**

Replace `twinkle/agentserver/tools/builtin/todo_tools.py` with:

```python
# twinkle/agentserver/tools/builtin/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

4 个 @tool: create / update / list / get。读 plan_todo_context 拿当前 session_id,
经 get_todo_store() 取共享 TodoStore 单例操作, 返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。mutation 后调用 append_todo_event() 发布 snapshot。
"""
from __future__ import annotations

from twinkle.agentserver.todo import (
    get_plan_todo_session_id,
    get_todo_store,
    append_todo_event,
    TodoError, TodoTask,
)
from twinkle.agentserver.tools.decorator import tool

_ICON = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}


def _format_tasks(tasks: list[TodoTask]) -> str:
    if not tasks:
        return "No todo tasks."
    lines = []
    for t in tasks:
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        deps = f" (blocked by: {', '.join(t.blocked_by[:6])}{'...' if len(t.blocked_by) > 6 else ''})" if t.blocked_by else ""
        owner = f" [@{t.owner}]" if t.owner else ""
        lines.append(f"- {icon} {t.subject}{deps}{owner}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks: list[TodoTask]) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


def _snapshot(tasks: list[TodoTask]) -> dict:
    """Structured todo snapshot for the UI (publish side-channel)."""
    pending_running = sum(1 for t in tasks if t.status in ("pending", "in_progress"))
    completed = sum(1 for t in tasks if t.status == "completed")
    return {
        "tasks": [
            {
                "id": t.id, "subject": t.subject, "description": t.description,
                "status": t.status, "result": t.result,
                "blocked_by": t.blocked_by, "owner": t.owner,
                "metadata": t.metadata,
                "created_at": t.created_at, "updated_at": t.updated_at,
            }
            for t in tasks
        ],
        "remaining": pending_running,
        "total": pending_running + completed,
    }


@tool
async def todo_create(subjects: list[str], sequential: bool = False) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task subjects; fails if a todo list already exists for this session. Use sequential=True when tasks must be executed in order.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        tasks = await store.create(session_id, subjects, sequential=sequential)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        seq_note = " (sequential)" if sequential else ""
        return _append_list(f"Created {len(tasks)} todo tasks{seq_note}.", current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_update(task_id: str, status: str = "", result: str = "", owner: str = "", metadata: dict | None = None) -> str:
    """Update a todo task's status, result, owner, or metadata. Use status="completed" to mark done, status="in_progress" to start working, status="cancelled" to cancel. Metadata is merged: set key to null to delete.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    try:
        kwargs = {}
        if status:
            kwargs["status"] = status
        if result:
            kwargs["result"] = result
        if owner:
            kwargs["owner"] = owner
        if metadata is not None:
            kwargs["metadata"] = metadata
        task, warning = await store.update(session_id, task_id, **kwargs)
        current = await store.list(session_id)
        append_todo_event(_snapshot(current))
        msg = f"Updated task {task_id}."
        if warning:
            msg += f"\n{warning}"
        return _append_list(msg, current)
    except TodoError as exc:
        current = await store.list(session_id)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list(status: str = "") -> str:
    """List all current todo tasks with their status. Optionally filter by status (pending/in_progress/completed/cancelled). Returns 'No todo tasks.' when empty.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    status_filter = status if status else None
    tasks = await store.list(session_id, status=status_filter)
    return _format_tasks(tasks)


@tool
async def todo_get(task_id: str) -> str:
    """Get details of a single todo task by its ID. Returns task info or error if not found.
    """
    session_id = get_plan_todo_session_id()
    store = get_todo_store()
    task = await store.get(session_id, task_id)
    if task is None:
        return f"Task {task_id} not found."
    lines = [
        f"ID: {task.id}",
        f"Subject: {task.subject}",
        f"Status: {task.status}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.result:
        lines.append(f"Result: {task.result}")
    if task.blocked_by:
        lines.append(f"Blocked by: {', '.join(task.blocked_by)}")
    if task.owner:
        lines.append(f"Owner: {task.owner}")
    if task.metadata:
        lines.append(f"Metadata: {task.metadata}")
    return "\n".join(lines)
```

- [ ] **Step 2: Update tool registration in `__init__.py`**

In `twinkle/agentserver/tools/__init__.py`, replace lines 28-30:

```python
    tm.register(todo_tools.todo_create)
    tm.register(todo_tools.todo_complete)
    tm.register(todo_tools.todo_list)
```

with:

```python
    tm.register(todo_tools.todo_create)
    tm.register(todo_tools.todo_update)
    tm.register(todo_tools.todo_list)
    tm.register(todo_tools.todo_get)
```

- [ ] **Step 3: Write tool test suite**

Replace `tests/test_todo_tools.py` with:

```python
# tests/test_todo_tools.py
import asyncio

import pytest
from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    _set_todo_store,
    flush_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.todo.store import TodoStore
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.todo_tools import todo_create, todo_update, todo_list, todo_get


@pytest.fixture(autouse=True)
def _isolated_todo_store(tmp_path):
    _set_todo_store(TodoStore(str(tmp_path / "todos")))
    yield
    _set_todo_store(None)


def _set_session_id(session_id: str) -> None:
    PLAN_TODO_SESSION_ID.set(session_id)


def test_create_returns_markdown_with_tasks() -> None:
    _set_session_id("tools-1")

    async def run():
        return await todo_create.invoke({"subjects": ["alpha", "beta"]})

    out = asyncio.run(run())
    assert "Created 2 todo tasks." in out
    assert "alpha" in out and "beta" in out
    assert "[ ]" in out


def test_create_sequential() -> None:
    _set_session_id("tools-seq")

    async def run():
        return await todo_create.invoke({"subjects": ["step1", "step2"], "sequential": True})

    out = asyncio.run(run())
    assert "Created 2 todo tasks (sequential)." in out
    assert "blocked by" in out


def test_update_marks_completed() -> None:
    _set_session_id("tools-2")
    tasks = asyncio.run(todo_create.invoke({"subjects": ["x", "y"]}))
    # Extract a task id from the store
    store = asyncio.run(todo_list.invoke({}))
    # Get the actual task id via the store
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-2"))
    task_id = all_tasks[0].id
    out = asyncio.run(todo_update.invoke({"task_id": task_id, "status": "completed", "result": "ok"}))
    assert "Updated task" in out
    assert "[x]" in out


def test_update_with_warning() -> None:
    _set_session_id("tools-warn")
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"], "sequential": True}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-warn"))
    blocked_id = all_tasks[1].id
    out = asyncio.run(todo_update.invoke({"task_id": blocked_id, "status": "in_progress"}))
    assert "Warning" in out


def test_create_twice_returns_error() -> None:
    _set_session_id("tools-3")
    asyncio.run(todo_create.invoke({"subjects": ["first"]}))
    out = asyncio.run(todo_create.invoke({"subjects": ["second"]}))
    assert "Error:" in out
    assert "already exists" in out


def test_update_unknown_task_error() -> None:
    _set_session_id("tools-4")
    asyncio.run(todo_create.invoke({"subjects": ["a"]}))
    out = asyncio.run(todo_update.invoke({"task_id": "nonexistent", "status": "completed"}))
    assert "Error:" in out
    assert "not found" in out


def test_list_empty_session() -> None:
    _set_session_id("tools-5-empty")

    async def run():
        return await todo_list.invoke({})

    out = asyncio.run(run())
    assert "No todo tasks." in out


def test_list_with_status_filter() -> None:
    _set_session_id("tools-filter")
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"]}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-filter"))
    asyncio.run(todo_update.invoke({"task_id": all_tasks[0].id, "status": "completed"}))
    out = asyncio.run(todo_list.invoke({"status": "completed"}))
    assert "a" in out
    assert "b" not in out


def test_get_found() -> None:
    _set_session_id("tools-get")
    asyncio.run(todo_create.invoke({"subjects": ["findme"]}))
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("tools-get"))
    out = asyncio.run(todo_get.invoke({"task_id": all_tasks[0].id}))
    assert "findme" in out


def test_get_not_found() -> None:
    _set_session_id("tools-getnf")
    out = asyncio.run(todo_get.invoke({"task_id": "nonexistent"}))
    assert "not found" in out


def test_sessions_isolated_via_contextvar() -> None:
    _set_session_id("iso-A")
    asyncio.run(todo_create.invoke({"subjects": ["A-task"]}))
    _set_session_id("iso-B")
    asyncio.run(todo_create.invoke({"subjects": ["B-task"]}))
    _set_session_id("iso-A")
    out = asyncio.run(todo_list.invoke({}))
    assert "A-task" in out
    assert "B-task" not in out


def test_schemas_registered_in_tool_manager() -> None:
    m = tool_manager()
    names = {t.card.name for t in m.list()}
    assert {"todo_create", "todo_update", "todo_list", "todo_get"} <= names
    schemas = {s["function"]["name"]: s for s in m.schemas()}
    assert "subjects" in schemas["todo_create"]["function"]["parameters"]["properties"]
    assert "sequential" in schemas["todo_create"]["function"]["parameters"]["properties"]
    assert "task_id" in schemas["todo_update"]["function"]["parameters"]["required"]


def test_create_publishes_snapshot() -> None:
    _set_session_id("pub-1")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["a", "b"]}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 2
    assert all(t["status"] == "pending" for t in snap["tasks"])
    assert snap["tasks"][0]["subject"] == "a"
    assert "id" in snap["tasks"][0]
    assert "blocked_by" in snap["tasks"][0]


def test_update_publishes_snapshot() -> None:
    _set_session_id("pub-2")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["x", "y"]}))
    flush_todo_events()
    from twinkle.agentserver.todo import get_todo_store
    all_tasks = asyncio.run(get_todo_store().list("pub-2"))
    asyncio.run(todo_update.invoke({"task_id": all_tasks[0].id, "status": "completed", "result": "ok"}))
    snapshots = flush_todo_events()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 1
    assert snap["tasks"][0]["status"] == "completed"


def test_list_does_not_publish() -> None:
    _set_session_id("pub-3")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["a"]}))
    flush_todo_events()
    asyncio.run(todo_list.invoke({}))
    assert flush_todo_events() == []


def test_error_path_does_not_publish() -> None:
    _set_session_id("pub-4")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["first"]}))
    flush_todo_events()
    asyncio.run(todo_create.invoke({"subjects": ["second"]}))
    assert flush_todo_events() == []
```

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/test_todo_store.py tests/test_todo_tools.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/builtin/todo_tools.py twinkle/agentserver/tools/__init__.py tests/test_todo_tools.py
git commit -m "feat: rewrite todo tools (create/update/list/get) + update registration"
```

---

### Task 4: 更新 Agent Loop system prompt

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py` (lines 113-117)

**Interfaces:**
- Consumes: New tool names from Task 3
- Produces: Updated system prompt visible to LLM

- [ ] **Step 1: Update system prompt**

In `twinkle/agentserver/agent_loop.py`, replace lines 113-117:

```python
## Todo（任务规划）
你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_complete、todo_list。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_complete(idx, result) 标记完成，调 todo_list 查看进度。
- 简单单步请求：直接回答或调工具，不要使用 todo。
```

with:

```python
## Todo（任务规划与追踪）
你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed") 标记完成。
- 有顺序依赖的任务：用 todo_create(subjects=[...], sequential=True)，系统自动串联依赖。
- 简单单步请求：直接回答或调工具，不要使用 todo。
```

- [ ] **Step 2: Verify no other references to todo_complete**

Run: `grep -r "todo_complete" twinkle/ --include="*.py" | grep -v __pycache__`
Expected: No matches (todo_complete is fully removed)

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_todo_store.py tests/test_todo_tools.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/agent_loop.py
git commit -m "feat: update agent loop system prompt for new todo tools"
```

---

### Task 5: 前端 — TypeScript 类型 + TodoPanel 增强

**Files:**
- Modify: `web/src/services/webClient.ts` (TodoTask interface)
- Modify: `web/src/composables/useSessions.ts` (TodoState type + box())
- Modify: `web/src/components/TodoPanel.vue` (status grouping + blocked_by + owner)

**Interfaces:**
- Consumes: WebSocket `todo.update` event payload with new fields (id, subject, description, blocked_by, owner, metadata, created_at, updated_at)
- Produces: Updated UI rendering

- [ ] **Step 1: Update TodoTask interface in webClient.ts**

In `web/src/services/webClient.ts`, replace lines 22-27:

```typescript
export interface TodoTask {
  idx: number
  title: string
  status: 'waiting' | 'running' | 'completed'
  result: string
}
```

with:

```typescript
export interface TodoTask {
  id: string
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
  result: string
  blocked_by: string[]
  owner: string
  metadata: Record<string, unknown>
  created_at: number
  updated_at: number
}
```

- [ ] **Step 2: Update useSessions.ts**

In `web/src/composables/useSessions.ts`, update the `box()` function (lines 58-62):

Replace:
```typescript
function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '◐'
  return '○'
}
```

with:

```typescript
function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'in_progress') return '◐'
  if (status === 'cancelled') return '✗'
  return '○'
}
```

- [ ] **Step 3: Rewrite TodoPanel.vue**

Replace `web/src/components/TodoPanel.vue` with:

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
import type { TodoTask } from '../services/webClient'

const { todo, completedCount, box } = useSessions()

function grouped(tasks: TodoTask[]) {
  const inProgress = tasks.filter(t => t.status === 'in_progress')
  const pending = tasks.filter(t => t.status === 'pending')
  const completed = tasks.filter(t => t.status === 'completed')
  const cancelled = tasks.filter(t => t.status === 'cancelled')
  return { inProgress, pending, completed, cancelled }
}
</script>

<template>
  <aside class="todo-panel">
    <div class="todo-head">
      <span>Todo</span>
      <span class="todo-count" v-if="todo">{{ completedCount }}/{{ todo.total }}</span>
    </div>
    <div v-if="todo && todo.tasks.length" class="todo-list">
      <!-- In Progress -->
      <div v-if="grouped(todo.tasks).inProgress.length" class="todo-group">
        <div class="todo-group-label"><span class="dot in-progress"></span>进行中</div>
        <div v-for="t in grouped(todo.tasks).inProgress" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-deps" v-if="t.blocked_by.length">依赖: {{ t.blocked_by.join(', ').slice(0, 30) }}</span>
          <span class="todo-owner" v-if="t.owner">@{{ t.owner }}</span>
        </div>
      </div>
      <!-- Pending -->
      <div v-if="grouped(todo.tasks).pending.length" class="todo-group">
        <div class="todo-group-label"><span class="dot pending"></span>待处理</div>
        <div v-for="t in grouped(todo.tasks).pending" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-deps" v-if="t.blocked_by.length">依赖: {{ t.blocked_by.join(', ').slice(0, 30) }}</span>
          <span class="todo-owner" v-if="t.owner">@{{ t.owner }}</span>
        </div>
      </div>
      <!-- Completed -->
      <div v-if="grouped(todo.tasks).completed.length" class="todo-group">
        <div class="todo-group-label"><span class="dot completed"></span>已完成</div>
        <div v-for="t in grouped(todo.tasks).completed" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
          <span class="todo-result" v-if="t.result">{{ t.result }}</span>
        </div>
      </div>
      <!-- Cancelled -->
      <div v-if="grouped(todo.tasks).cancelled.length" class="todo-group">
        <div class="todo-group-label"><span class="dot cancelled"></span>已取消</div>
        <div v-for="t in grouped(todo.tasks).cancelled" :key="t.id" :class="['todo-item', t.status]">
          <span class="todo-box">{{ box(t.status) }}</span>
          <span class="todo-subject">{{ t.subject }}</span>
        </div>
      </div>
    </div>
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
.todo-group { margin-bottom: .75rem; }
.todo-group-label { display: flex; align-items: center; gap: .4rem; font-size: .75rem; font-weight: 600; color: #64748b; padding: .25rem .25rem .15rem; text-transform: uppercase; letter-spacing: .05em; }
.dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.dot.in-progress { background: #6366f1; animation: pulse 1.5s ease-in-out infinite; }
.dot.pending { background: #94a3b8; }
.dot.completed { background: #10b981; }
.dot.cancelled { background: #ef4444; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
.todo-item { display: flex; flex-wrap: wrap; align-items: baseline; gap: .25rem; padding: .3rem .25rem; font-size: .85rem; }
.todo-item.completed .todo-subject { text-decoration: line-through; color: #94a3b8; }
.todo-item.cancelled .todo-subject { text-decoration: line-through; color: #ef4444; }
.todo-item.in-progress .todo-subject { color: #4f46d5; font-weight: 500; }
.todo-box { width: 1.1em; text-align: center; color: #4f46d5; flex-shrink: 0; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-item.cancelled .todo-box { color: #ef4444; }
.todo-subject { flex: 1; min-width: 0; }
.todo-deps { font-size: .7rem; color: #f59e0b; background: #fef3c7; padding: 0 .35rem; border-radius: 3px; }
.todo-owner { font-size: .7rem; color: #6366f1; }
.todo-result { color: #64748b; font-size: .75rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
</style>
```

- [ ] **Step 4: Verify frontend compiles**

Run: `cd web && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: No type errors

- [ ] **Step 5: Commit**

```bash
git add web/src/services/webClient.ts web/src/composables/useSessions.ts web/src/components/TodoPanel.vue
git commit -m "feat: update frontend TodoTask type + TodoPanel with status grouping and dependencies"
```

---

### Task 6: 清理旧引用 + 端到端验证

**Files:**
- Modify: `tests/test_tool_manager.py:88-95` (replace `todo_complete` with `todo_update` + `todo_get`)
- Modify: `twinkle/config/schema.py:121-123` (replace `todo_complete` with `todo_update` + `todo_get`)
- Verify: all tests pass

- [ ] **Step 1: Update test_tool_manager.py**

In `tests/test_tool_manager.py`, replace lines 91-95:

```python
    assert {
        "todo_create",
        "todo_complete",
        "todo_list",
    } <= names
```

with:

```python
    assert {
        "todo_create",
        "todo_update",
        "todo_list",
        "todo_get",
    } <= names
```

- [ ] **Step 2: Update config/schema.py**

In `twinkle/config/schema.py`, replace lines 121-123:

```python
        "todo_create": "allow",
        "todo_complete": "allow",
        "todo_list": "allow",
```

with:

```python
        "todo_create": "allow",
        "todo_update": "allow",
        "todo_list": "allow",
        "todo_get": "allow",
```

- [ ] **Step 3: Check for any remaining references to old API**

Run: `grep -rn "todo_complete\|\.idx\|\.title" tests/ twinkle/ --include="*.py" | grep -v __pycache__ | grep -v test_todo_store | grep -v test_todo_tools | grep -v "todo/store.py" | grep -v "todo_tools.py"`

Expected: No matches

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_tool_manager.py twinkle/config/schema.py
git commit -m "chore: clean up old todo_complete references in test_tool_manager and config schema"
```

- [ ] **Step 6: Final commit — update roadmap**

In `roadmap.md`, add Phase 8 to the status section and mark it as in progress.

```
- **Phase 8（Todo 增强）**：初版已落地（spec `docs/superpowers/specs/2026-08-01-todo-enhancement-design.md`）。TodoTask 数据模型增强（id、subject、description、blocked_by、owner、metadata）；4 个工具重写（todo_create/update/list/get）；`sequential=True` 一步创建线性依赖；轻量守卫（跳步检测 + 提醒）；前端按状态分组 + 依赖/归属展示。对应里程碑 M9 ✅。
```

```bash
git add roadmap.md
git commit -m "docs: mark Phase 8 todo enhancement as landed in roadmap"
```
