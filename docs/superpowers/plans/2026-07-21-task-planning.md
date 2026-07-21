# Task Planning (todo 工具) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Twinkle 的扁平 ReAct loop 加上 jiuwenclaw 风格的轻量任务规划——常驻 3 个 todo 工具(create/complete/list)+ ContextVar session 路由 + 会话首次注入 system message,模型自行判断是否拆任务。

**Architecture:** 复用现有四层工具系统(`@tool` + `LocalFunction` + `ToolManager`),todo 工具经 `ToolManager.schemas()`/`execute()` 接入,`agent_loop` 零接口改动。session 路由用 `ContextVar`(`run_stream` 入口 set,工具读),不改 `Tool` 接口签名。内存 `dict[session_id, list[TodoTask]]` 存储 + `asyncio.Lock`,与 SessionStore 哲学一致。

**Tech Stack:** Python 3.10+(`X | None` PEP 604 已用),stdlib only(无新依赖)。测试用 `asyncio.run()` + `pytest`,**不用 pytest-asyncio**。

## Global Constraints

- 测试**不得**依赖 `pytest-asyncio`;用 `asyncio.run()` 驱动 async,与 `tests/conftest.py` 的 `free_port`/`port_factory` 风格一致(参考 `tests/test_web_fetch.py`、`tests/test_tool_manager.py`)。
- 工具用 `@tool` 装饰,schema 自动从签名+docstring 派生,**不**用 `input_params=` 覆盖(与 `web_fetch` 等现有工具一致)。
- `ToolManager.execute(name, args) -> str` 签名**不变**;todo 工具靠 ContextVar 拿 session_id,不通过 execute 参数传。
- 提交信息用 `Phase 2:` 前缀(对齐近期 commit 风格,见 `git log`)。每个 Task 结尾 commit 一次。
- 平台:Windows,shell 为 bash;`python` 命令默认指项目 venv 的 `.venv/Scripts/python.exe`。

---

## File Structure

| 文件 | 责任 | 新建/修改 |
|---|---|---|
| `twinkle/agentserver/plan_todo_context.py` | `ContextVar` 持当前请求 session_id + getter | 新建 |
| `twinkle/agentserver/todo_store.py` | `TodoTask` dataclass + `TodoError` + `TodoStore`(内存 dict + asyncio.Lock) | 新建 |
| `twinkle/agentserver/tools/todo_tools.py` | 3 个 `@tool` async 函数(create/complete/list),读 ContextVar、操作 TodoStore、返回 markdown 串 | 新建 |
| `twinkle/agentserver/tools/__init__.py` | `tool_manager()` 注册 3 个 todo 工具 | 修改 |
| `twinkle/agentserver/agent_loop.py` | `run_stream` 入口 set ContextVar + 会话首次插 system message | 修改 |
| `tests/test_plan_todo_context.py` | ContextVar get/set 测试 | 新建 |
| `tests/test_todo_store.py` | TodoStore 业务逻辑 + 并发测试 | 新建 |
| `tests/test_todo_tools.py` | 3 个工具经 ContextVar 路由 + schema 形状 + session 隔离 | 新建 |
| `tests/test_agent_loop.py` | 既有用例改断言(系统消息在前)+ 新增 todo 经 loop 的 round-trip 用例 | 修改 |

---

### Task 1: plan_todo_context.py — ContextVar session 路由

**Files:**
- Create: `twinkle/agentserver/plan_todo_context.py`
- Test: `tests/test_plan_todo_context.py`

**Interfaces:**
- Produces: `PLAN_TODO_SESSION_ID: contextvars.ContextVar[str]`(default `"default"`);`get_plan_todo_session_id() -> str`(取不到返回 `"default"`,不抛)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_todo_context.py
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    get_plan_todo_session_id,
)


def test_default_is_default_string() -> None:
    # No token set in this fresh context -> "default".
    PLAN_TODO_SESSION_ID.set(None)
    assert get_plan_todo_session_id() == "default"


def test_returns_set_session_id() -> None:
    PLAN_TODO_SESSION_ID.set("sess-abc")
    assert get_plan_todo_session_id() == "sess-abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan_todo_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.plan_todo_context'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/plan_todo_context.py
"""Plan-todo 的当前请求 session 上下文。

由 AgentLoop.run_stream 在每次请求入口写入,使无参的 todo 工具能在
当前会话上下文中定位到对应的 todo 列表。对齐 jiuwenclaw
agentserver/plan_todo_context.py(仅保留 ContextVar + getter,
砍掉 team session 解析等 Twinkle 没有的依赖)。
"""
from __future__ import annotations

import contextvars

PLAN_TODO_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "twinkle_plan_todo_session_id",
    default="default",
)


def get_plan_todo_session_id() -> str:
    """当前请求应使用的 session id;未设置时返回 "default"(不抛异常)。"""
    return PLAN_TODO_SESSION_ID.get() or "default"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan_todo_context.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/plan_todo_context.py tests/test_plan_todo_context.py
git commit -m "Phase 2: plan_todo_context (ContextVar session 路由)"
```

---

### Task 2: todo_store.py — 内存 TodoStore

**Files:**
- Create: `twinkle/agentserver/todo_store.py`
- Test: `tests/test_todo_store.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `TodoTask` dataclass:`idx: int, title: str, status: str, result: str`(`status ∈ {"waiting","running","completed"}`)。
  - `TodoError(Exception)`:业务错误,消息可直接展示给模型。
  - `TodoStore`:`async create(session_id, tasks: list[str]) -> list[TodoTask]`、`async complete(session_id, idx: int, result: str = "") -> list[TodoTask]`、`async list_tasks(session_id) -> list[TodoTask]`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_todo_store.py
import asyncio

import pytest

from twinkle.agentserver.todo_store import TodoError, TodoStore


def test_create_then_list() -> None:
    store = TodoStore()

    async def run():
        created = await store.create("s1", ["a", "b"])
        listed = await store.list_tasks("s1")
        return created, listed

    created, listed = asyncio.run(run())
    assert [t.idx for t in created] == [1, 2]
    assert [t.title for t in created] == ["a", "b"]
    assert all(t.status == "waiting" for t in created)
    assert listed == created


def test_create_empty_raises() -> None:
    store = TodoStore()
    with pytest.raises(TodoError, match="non-empty"):
        asyncio.run(store.create("s1", []))


def test_create_twice_raises_already_exists() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="already exists"):
        asyncio.run(store.create("s1", ["b"]))


def test_complete_marks_status_and_result() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a", "b"]))
    tasks = asyncio.run(store.complete("s1", 1, result="done A"))
    assert tasks[0].status == "completed"
    assert tasks[0].result == "done A"
    assert tasks[1].status == "waiting"


def test_complete_unknown_idx_raises() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    with pytest.raises(TodoError, match="not found"):
        asyncio.run(store.complete("s1", 99))


def test_complete_already_completed_raises() -> None:
    store = TodoStore()
    asyncio.run(store.create("s1", ["a"]))
    asyncio.run(store.complete("s1", 1))
    with pytest.raises(TodoError, match="already completed"):
        asyncio.run(store.complete("s1", 1))


def test_sessions_isolated() -> None:
    store = TodoStore()
    asyncio.run(store.create("sA", ["a"]))
    asyncio.run(store.create("sB", ["b"]))
    assert [t.title for t in asyncio.run(store.list_tasks("sA"))] == ["a"]
    assert [t.title for t in asyncio.run(store.list_tasks("sB"))] == ["b"]


def test_concurrent_complete_no_lost_update() -> None:
    """Two coroutines completing different tasks on the same session
    must both succeed (no lost update from read-modify-write)."""
    store = TodoStore()
    asyncio.run(store.create("s1", ["a", "b"]))

    async def run():
        await asyncio.gather(
            store.complete("s1", 1, result="A"),
            store.complete("s1", 2, result="B"),
        )
        return await store.list_tasks("s1")

    tasks = asyncio.run(run())
    assert all(t.status == "completed" for t in tasks)
    assert {t.result for t in tasks} == {"A", "B"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.todo_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/todo_store.py
"""TodoStore — agent 内部任务规划的内存存储。

dict[session_id, list[TodoTask]] + 每 session 一把 asyncio.Lock,
串行化 read-modify-write 防丢更新。对齐 jiuwenclaw
agentserver/tools/todo_toolkits.py 的 TodoToolkit,但:
- 存内存(不写 todo.md),与 Twinkle SessionStore 哲学一致;
- 只保留 create/complete/list_tasks(砍 start/insert/remove/batch);
- 砍 op-result 发布总线(Twinkle 无 rail 消费)。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class TodoTask:
    idx: int
    title: str
    status: str  # "waiting" | "running" | "completed"
    result: str = ""


class TodoError(Exception):
    """业务级错误,消息可直接回给模型。"""


class TodoStore:
    def __init__(self) -> None:
        self._data: dict[str, list[TodoTask]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        # 单线程事件循环下 setdefault 无竞态(同步调用,无 await 间隙)。
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def create(self, session_id: str, tasks: list[str]) -> list[TodoTask]:
        if not tasks:
            raise TodoError("tasks must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._data.get(session_id, [])
            if existing:
                raise TodoError(
                    f"todo list already exists for session {session_id}."
                )
            new = [
                TodoTask(idx=i + 1, title=t, status="waiting", result="")
                for i, t in enumerate(tasks)
            ]
            self._data[session_id] = new
            return list(new)

    async def complete(
        self, session_id: str, idx: int, result: str = ""
    ) -> list[TodoTask]:
        async with self._lock(session_id):
            tasks = self._data.get(session_id, [])
            for t in tasks:
                if t.idx == idx:
                    if t.status == "completed":
                        raise TodoError(f"Task {idx} is already completed.")
                    t.status = "completed"
                    t.result = (result or "").strip() or "done"
                    return list(tasks)
            raise TodoError(f"Task {idx} not found.")

    async def list_tasks(self, session_id: str) -> list[TodoTask]:
        async with self._lock(session_id):
            return list(self._data.get(session_id, []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_todo_store.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/todo_store.py tests/test_todo_store.py
git commit -m "Phase 2: TodoStore 内存存储 (create/complete/list_tasks + asyncio.Lock)"
```

---

### Task 3: tools/todo_tools.py — 3 个 @tool 工具 + 注册

**Files:**
- Create: `twinkle/agentserver/tools/todo_tools.py`
- Modify: `twinkle/agentserver/tools/__init__.py`
- Test: `tests/test_todo_tools.py`

**Interfaces:**
- Consumes: `get_plan_todo_session_id()`(Task 1)、`TodoStore` / `TodoError`(Task 2)、`@tool`(现有)。
- Produces: `todo_create`、`todo_complete`、`todo_list` 三个 `LocalFunction`(经 `@tool`),名为 `todo_create`/`todo_complete`/`todo_list`;模块级单例 `_store: TodoStore`。`tool_manager()` 现在也注册这三个。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_todo_tools.py
import asyncio

from twinkle.agentserver.plan_todo_context import PLAN_TODO_SESSION_ID
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.todo_tools import todo_complete, todo_create, todo_list


def _set_sid(sid: str) -> None:
    PLAN_TODO_SESSION_ID.set(sid)


def test_create_returns_markdown_with_tasks() -> None:
    _set_sid("tools-1")

    async def run():
        return await todo_create.invoke({"tasks": ["alpha", "beta"]})

    out = asyncio.run(run())
    assert "Created 2 todo tasks." in out
    assert "alpha" in out and "beta" in out
    assert "[ ]" in out  # waiting checkbox


def test_complete_marks_and_lists() -> None:
    _set_sid("tools-2")
    asyncio.run(todo_create.invoke({"tasks": ["x", "y"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 1, "result": "ok"}))
    assert "Task 1 marked as completed." in out
    assert "[x]" in out  # completed checkbox
    assert "ok" in out


def test_create_twice_returns_error_with_current_list() -> None:
    _set_sid("tools-3")
    asyncio.run(todo_create.invoke({"tasks": ["first"]}))
    out = asyncio.run(todo_create.invoke({"tasks": ["second"]}))
    assert "Error:" in out
    assert "already exists" in out
    assert "first" in out  # current list appended
    assert "second" not in out


def test_complete_unknown_idx_error() -> None:
    _set_sid("tools-4")
    asyncio.run(todo_create.invoke({"tasks": ["a"]}))
    out = asyncio.run(todo_complete.invoke({"idx": 9}))
    assert "Error:" in out
    assert "not found" in out


def test_list_empty_session() -> None:
    _set_sid("tools-5-empty")

    async def run():
        return await todo_list.invoke({})

    out = asyncio.run(run())
    assert "No todo tasks." in out


def test_sessions_isolated_via_contextvar() -> None:
    _set_sid("iso-A")
    asyncio.run(todo_create.invoke({"tasks": ["A-task"]}))
    _set_sid("iso-B")
    asyncio.run(todo_create.invoke({"tasks": ["B-task"]}))
    _set_sid("iso-A")
    out = asyncio.run(todo_list.invoke({}))
    assert "A-task" in out
    assert "B-task" not in out


def test_schemas_registered_in_tool_manager() -> None:
    m = tool_manager()
    names = {t.card.name for t in m.list()}
    assert {"todo_create", "todo_complete", "todo_list"} <= names
    schemas = {s["function"]["name"]: s for s in m.schemas()}
    assert schemas["todo_create"]["function"]["parameters"]["properties"]["tasks"][
        "type"
    ] == "array"
    assert "tasks" in schemas["todo_create"]["function"]["parameters"]["required"]
    # idx required for complete; result optional (has default)
    complete_req = schemas["todo_complete"]["function"]["parameters"]["required"]
    assert "idx" in complete_req
    assert "result" not in complete_req
```

> 注:`todo_create.invoke({"tasks": ...})` —— `LocalFunction.invoke` 接 kwargs dict;`@tool` 装饰出的就是 `LocalFunction`,所以直接 `.invoke()`。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.tools.todo_tools'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/tools/todo_tools.py
"""Todo 工具 — agent 内部任务规划的对外接口。

3 个 @tool:create / complete / list。读 plan_todo_context 拿当前
session_id,操作模块级 TodoStore 单例,返回 markdown 串(附当前列表,
省一次 todo_list round-trip)。业务错误 catch 成 "Error: ..." 字符串
返回(虽然 ToolManager.execute 也兜底,但工具层自转更可读)。

对齐 jiuwenclaw tools/todo_toolkits.py,砍 start/insert/remove/batch
与 op-result 总线。
"""
from __future__ import annotations

from twinkle.agentserver.plan_todo_context import get_plan_todo_session_id
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.todo_store import TodoError, TodoStore

_store = TodoStore()  # 模块级单例;session 隔离靠 ContextVar 路由

_ICON = {"waiting": "[ ]", "running": "[>]", "completed": "[x]"}


def _format_tasks(tasks) -> str:
    if not tasks:
        return "No todo tasks."
    lines = []
    for t in tasks:
        icon = _ICON.get(t.status, "[ ]")
        suffix = f" | {t.result}" if t.result else ""
        lines.append(f"- {icon} {t.idx}. {t.title}{suffix}")
    return "\n".join(lines)


def _append_list(message: str, tasks) -> str:
    return f"{message}\n\nCurrent todo list:\n{_format_tasks(tasks)}"


@tool
async def todo_create(tasks: list[str]) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task descriptions; fails if a todo list already exists for this session.
    """
    sid = get_plan_todo_session_id()
    try:
        created = await _store.create(sid, tasks)
        return _append_list(f"Created {len(created)} todo tasks.", created)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_complete(idx: int, result: str = "") -> str:
    """Mark a todo task as completed and save a brief result. Pass the 1-based idx and an optional short result string.
    """
    sid = get_plan_todo_session_id()
    try:
        tasks = await _store.complete(sid, idx, result)
        return _append_list(f"Task {idx} marked as completed.", tasks)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)


@tool
async def todo_list() -> str:
    """List all current todo tasks with their status. Returns 'No todo tasks.' when empty.
    """
    sid = get_plan_todo_session_id()
    tasks = await _store.list_tasks(sid)
    return _format_tasks(tasks)
```

- [ ] **Step 4: Register the three tools in the default manager**

Modify `twinkle/agentserver/tools/__init__.py`: after the existing three `tm.register(...)` lines, add the todo tools. The final `tool_manager()` function body becomes:

```python
def tool_manager() -> ToolManager:
    """Build a ToolManager pre-loaded with the default tools."""
    tm = ToolManager()
    tm.register(tool(web_fetch.web_fetch))
    tm.register(tool(web_search.web_search))
    tm.register(tool(command_exec.command_exec))
    tm.register(todo_tools.todo_create)
    tm.register(todo_tools.todo_complete)
    tm.register(todo_tools.todo_list)
    return tm
```

And add the import at the top of the file (after the existing `from twinkle.agentserver.tools import command_exec, web_fetch, web_search` line):

```python
from twinkle.agentserver.tools import todo_tools
```

> 注:`todo_tools.todo_create` 已经是 `@tool` 装饰出的 `LocalFunction`,直接 `tm.register(...)`,**不再**包一层 `tool(...)`。

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/tools/todo_tools.py twinkle/agentserver/tools/__init__.py tests/test_todo_tools.py
git commit -m "Phase 2: todo 工具 (create/complete/list) + 注册进 tool_manager"
```

---

### Task 4: agent_loop.py — ContextVar set + 会话首次注入 system message

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py`
- Modify: `tests/test_agent_loop.py`(既有断言要随系统消息前移而更新 + 新增 todo 经 loop 的 round-trip 用例)

**Interfaces:**
- Consumes: `PLAN_TODO_SESSION_ID`(Task 1)、`todo_create/complete/list` 已注册(Task 3)。
- Produces:`run_stream` 现在每请求开头 set ContextVar 并在会话首次插 system message。

> **关键**:既有 `test_agent_loop.py` 断言 `msgs[0]["role"] == "user"`(line 93)、`len(seen_messages[0]) == 1`(line 131)、`len(seen_messages[1]) == 3`(line 132)——注入系统消息后这些要前移一位。本 Task 必须同步改这些断言,否则既有用例会挂。

- [ ] **Step 1: Update existing tests to expect the system message**

Open `tests/test_agent_loop.py`. The system message is prepended once per session at the start of `run_stream`, before the user message. Update these assertions:

In `test_tool_call_round_trip_then_answer` (around line 92-97), shift indices by 1 and assert the new system message:

```python
    # session store now holds: system, user, assistant(tool_calls), tool, assistant(answer)
    msgs = store.get_messages("s1")
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant" and msgs[2]["tool_calls"]
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "c1"
    assert msgs[3]["content"] == "tool-saw:hi"
    assert msgs[4]["role"] == "assistant"
```

In `test_cross_turn_remembers_context` (around line 131-135), the system message is inserted once (turn 1) and persists; turn 2 does not re-insert:

```python
    # turn 2's messages include turn 1's user + assistant, plus the system msg from turn 1
    assert len(seen_messages[0]) == 2   # [system, user]
    assert len(seen_messages[1]) == 4   # [system, user, assistant, user]
    assert seen_messages[0][0]["role"] == "system"
    assert seen_messages[1][1]["content"] == "turn1"
    assert seen_messages[1][2]["content"] == "ack1"
    assert seen_messages[1][3]["content"] == "turn2"
```

In `test_plain_answer_streams_chunks_and_complete` and `test_max_steps_emits_error`: no index assertions on store messages, so no change needed — but confirm they still pass after the wiring change.

- [ ] **Step 2: Add a new failing test for the todo round-trip through the loop**

Append to `tests/test_agent_loop.py`:

```python
def test_todo_create_round_trip_through_loop() -> None:
    """Model calls todo_create then answers — proves ContextVar is set
    (todo tool could not resolve session_id otherwise) and the system
    message is present."""
    from twinkle.agentserver.tools import tool_manager

    store = SessionStore()
    llm = _ScriptedLLM([
        # turn 1: model calls todo_create
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "tc1", "type": "function",
                              "function": {"name": "todo_create",
                                           "arguments": '{"tasks": ["step one", "step two"]}'}}]})],
        # turn 2: model answers
        [TextDelta("planned "), TextDelta("it"),
         Finish("stop", {"role": "assistant", "content": "planned it", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, tool_manager(), LongTermMemory())

    async def run():
        return [f async for f in loop.run_stream(_env("plan something", session_id="s-todo"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"
    # tool result was re-injected into the store
    msgs = store.get_messages("s-todo")
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant" and msgs[2]["tool_calls"]
    assert msgs[3]["role"] == "tool"
    assert "Created 2 todo tasks." in msgs[3]["content"]
    assert "step one" in msgs[3]["content"]
    assert msgs[4]["role"] == "assistant" and msgs[4]["content"] == "planned it"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL — updated assertions find `user` where they now expect `system`; the new todo round-trip test fails because `run_stream` doesn't yet set the ContextVar / insert the system message (todo tool resolves "default" session, store stays empty, "Created 2" not present) and because `msgs[0]` is `user` not `system`.

- [ ] **Step 4: Write minimal implementation in agent_loop.py**

At the top of `twinkle/agentserver/agent_loop.py`, add the imports and the system prompt constant after the existing imports (after `from twinkle.e2a.models import ...`):

```python
from twinkle.agentserver.plan_todo_context import PLAN_TODO_SESSION_ID

TODO_SYSTEM_PROMPT = (
    "You have todo tools to plan and track multi-step work: "
    "todo_create, todo_complete, todo_list. For non-trivial multi-step "
    "requests, first call todo_create with a list of sub-tasks, then work "
    "through them calling todo_complete(idx, result) as each finishes, and "
    "call todo_list to check progress. For simple one-step requests, do NOT "
    "use the todo tools — just answer or call the needed tool directly."
)
```

Then modify the top of `run_stream`. The current opening is:

```python
    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        session_id = envelope.session_id
        query = (envelope.params or {}).get("query", "")
        self._store.append(session_id, {"role": "user", "content": query})
        # long-term memory stub: recall is a no-op in Phase 1; shape preserved.
        self._memory.recall(query)
```

Replace it with (set ContextVar + first-insert system message before appending user):

```python
    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        session_id = envelope.session_id
        PLAN_TODO_SESSION_ID.set(session_id or "default")
        # Insert the todo-guidance system message once per session (first call),
        # so the model knows when/how to use the todo tools. Re-runs in the same
        # session see it already present and skip insertion — no accumulation.
        existing = self._store.get_messages(session_id)
        if not existing or existing[0].get("role") != "system":
            self._store.append(session_id, {"role": "system", "content": TODO_SYSTEM_PROMPT})
        query = (envelope.params or {}).get("query", "")
        self._store.append(session_id, {"role": "user", "content": query})
        # long-term memory stub: recall is a no-op in Phase 1; shape preserved.
        self._memory.recall(query)
```

The rest of `run_stream` (the `for _step in range(MAX_STEPS)` loop) is unchanged — tool execution + result re-injection already handle todo tools via `ToolManager.execute`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: PASS (all existing tests updated + the new `test_todo_create_round_trip_through_loop`)

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/agent_loop.py tests/test_agent_loop.py
git commit -m "Phase 2: agent_loop set ContextVar + 会话首次注入 todo system message"
```

---

### Task 5: Full suite verification

**Files:** 无(仅跑测试)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — all existing tests + new `test_plan_todo_context.py` / `test_todo_store.py` / `test_todo_tools.py` / new `test_agent_loop.py` case.

> 若 `test_integration.py` 或 `test_agentserver_handler.py` 因 system message 改动而失败:检查它们是否对 store 消息顺序/计数有断言;若有则按 Task 4 同法前移一位。这两条不预期失败(它们走 ws 链路,断言多在 E2A frame 层),但若挂了就照此修。

- [ ] **Step 2: Sanity-run the production wiring import**

Run: `python -c "from twinkle.agentserver.server import agent_loop; l = agent_loop(); print(sorted(t.card.name for t in l._tools.list()))"`
Expected: prints a list including `command_exec, todo_complete, todo_create, todo_list, web_fetch, web_search` (proves the default manager wires todo tools without import errors).

- [ ] **Step 3: Commit (if any test fixes from Step 1)**

```bash
git add -A
git commit -m "Phase 2: test suite green after task planning wiring"
```

> 若 Step 1 全绿无改动,此步可跳过(工作树已 clean)。

---

## Self-Review(已执行)

- **Spec coverage**:spec §1 目标(3 工具)→ Task 3;§1 内存存储 → Task 2;§1 ContextVar 路由 → Task 1 + Task 4 set;§1 会话首次 system message → Task 4;§2 组件全部映射到 File Structure;§3 数据流 → Task 4 注释 + Task 4 新测试覆盖 todo round-trip;§4 错误处理 → Task 2 `TodoError` + Task 3 工具层 catch + `ToolManager.execute` 既有兜底;§5 测试 → Task 1-4 各自测试 + Task 5 全量。`clear` 明确不做(Global Constraints + 无 Task 涉及)。
- **Placeholder scan**:无 TBD/TODO;"appropriate error handling" 类泛化无(每步有完整代码与断言)。
- **Type consistency**:`TodoStore.create/complete/list_tasks` 在 Task 2 定义,Task 3 调用名一致;`get_plan_todo_session_id()` Task 1 定义 Task 3 调用一致;`TodoTask` 字段(idx/title/status/result)Task 2 定义 Task 3 `_format_tasks` 读取一致;`PLAN_TODO_SESSION_ID` Task 1 定义 Task 4 set 一致。
