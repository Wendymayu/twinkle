# Todo Progress UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 todo 工具执行时的任务列表/进度实时流到浏览器,渲染成右侧侧边面板(checkbox 列表 + N/M 进度)。

**Architecture:** 轻量事件总线——todo 工具 publish 结构化快照到 per-request ContextVar list;`agent_loop` 每次 `tools.execute` 后 drain,yield `e2a.todo_update` frame;`MessageHandler` 按 `response_kind` 分支 emit `todo.update` 浏览器事件(结构化 payload);前端 `webClient` + `App.vue` 侧栏渲染。不建 rail,不动 chat delta/final 链路,不动 `ToolManager.execute -> str` 契约。

**Tech Stack:** Python 3.10+ stdlib(contextvars);前端 Vue 3 `<script setup>` + TS(无新依赖)。测试 `asyncio.run()` + pytest,**不用 pytest-asyncio**。前端无单测,手动验证。

## Global Constraints

- 测试**不得**依赖 `pytest-asyncio`;用 `asyncio.run()`(参考 `tests/test_todo_tools.py` 风格)。
- `ToolManager.execute(name, args) -> str` 契约**不变**;结构化数据走 ContextVar 侧信道。
- chat 的 delta/final 链路**不动**;`todo.update` 是新增的并行事件,不替换任何现有 frame。
- 不建 rail 框架;不引入 `todo_clear`;前端不写单测。
- 提交信息用 `Phase 2:` 前缀。每 Task 结尾 commit 一次。
- 平台 Windows / bash;`python` 指项目 venv。
- 既有 `test_todo_tools.py` / `test_plan_todo_context.py` 的现有用例**不能挂**:注意 `publish_todo_update` 在 bus 未初始化(未调 `reset_todo_events()`)时必须 no-op,否则这些现有用例(直接 invoke 工具、不经 run_stream)会行为变化。

---

## File Structure

| 文件 | 责任 | 新建/修改 |
|---|---|---|
| `twinkle/agentserver/plan_todo_context.py` | + `TODO_EVENTS` ContextVar + `reset_todo_events` / `publish_todo_update` / `drain_todo_events` | 修改 |
| `twinkle/agentserver/tools/todo_tools.py` | create/complete 成功路径 publish 快照;加 `_snapshot` helper | 修改 |
| `twinkle/agentserver/agent_loop.py` | run_stream 入口 `reset_todo_events`;每次 execute 后 drain → yield e2a.todo_update | 修改 |
| `twinkle/e2a/models.py` | `response_kind` 注释加 `e2a.todo_update` | 修改(注释) |
| `twinkle/schema/message.py` | `EventType.TODO_UPDATE = "todo.update"` | 修改 |
| `twinkle/gateway/message_handler.py` | `_process_stream` 按 `response_kind` 分支 | 修改 |
| `web/src/services/webClient.ts` | + `TodoUpdateHandler` + `todo.update` 事件分发 | 修改 |
| `web/src/App.vue` | + 右侧 TodoPanel + 两栏布局 | 修改 |
| `tests/test_plan_todo_context.py` | + publish/drain 用例 | 修改 |
| `tests/test_todo_tools.py` | + create/complete publish / list 不 publish / 错误不 publish | 修改 |
| `tests/test_agent_loop.py` | + e2a.todo_update frame 断言 | 修改 |
| `tests/test_message_handler.py` | 新建:fake AgentClient → todo.update Message | 新建 |

---

### Task 1: plan_todo_context.py — 事件总线

**Files:**
- Modify: `twinkle/agentserver/plan_todo_context.py`
- Test: `tests/test_plan_todo_context.py`

**Interfaces:**
- Produces:
  - `TODO_EVENTS: contextvars.ContextVar[list[dict] | None]`(default `None`)。
  - `reset_todo_events() -> None`:`TODO_EVENTS.set([])`。
  - `publish_todo_update(snapshot: dict) -> None`:bus 为 `None` 时 no-op;否则 `TODO_EVENTS.get().append(snapshot)`。
  - `drain_todo_events() -> list[dict]`:bus 为 `None` 或空时返回 `[]`;否则拷贝当前 list、原地清空、返回拷贝。

- [ ] **Step 1: Write the failing tests** (append to `tests/test_plan_todo_context.py`)

```python
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    get_plan_todo_session_id,
    publish_todo_update,
    reset_todo_events,
)


def test_publish_then_drain() -> None:
    reset_todo_events()
    publish_todo_update({"tasks": [], "remaining": 0, "total": 0})
    publish_todo_update({"tasks": [{"idx": 1}], "remaining": 1, "total": 1})
    drained = drain_todo_events()
    assert len(drained) == 2
    assert drained[0]["total"] == 0
    assert drained[1]["remaining"] == 1
    # drain cleared the bus
    assert drain_todo_events() == []


def test_publish_without_reset_is_noop() -> None:
    # Bus uninitialized (None) -> publish must not raise and must not pollute.
    PLAN_TODO_SESSION_ID.set(None)  # unrelated; just reset the sid var
    # NOTE: TODO_EVENTS may carry state from a prior test; we only assert
    # publish does not raise when the bus was never reset in THIS context.
    publish_todo_update({"tasks": [], "remaining": 0, "total": 0})
    # No assertion on contents here — the contract is "no raise".


def test_drain_without_reset_returns_empty() -> None:
    # A fresh ContextVar default is None -> drain returns [].
    # (ContextVar default applies when no .set has occurred in this context.)
    assert drain_todo_events() == []
```

> 注:`test_publish_without_reset_is_noop` 与 `test_drain_without_reset_returns_empty` 依赖 ContextVar 在该测试上下文里未被 set 过(default None)。pytest 各 test 间不共享 ContextVar set(ContextVar 的 set 作用域是当前 context,pytest 默认每个 test 跑在主 context,`set` 会持续到被覆盖——所以 `test_publish_then_drain` 之后 TODO_EVENTS 可能非 None)。因此这两个「未 reset」用例必须能容忍前序测试的污染:`publish_todo_update` 在 `TODO_EVENTS.get()` 返回非 None list 时会 append——这会让 `test_publish_without_reset_is_noop` 实际上往一个旧 list 里 append。为避免测试间耦合,**在 `test_publish_without_reset_is_noop` 开头显式 `reset_todo_events()` 之前先清掉**。但那样就不是「未 reset」场景了。真正干净的写法:这两个用例用 `contextvars.copy_context().run(...)` 在隔离 context 里跑。改用如下更稳健形式:

```python
import contextvars


def test_publish_without_reset_is_noop() -> None:
    # In a fresh isolated context, TODO_EVENTS is its default (None);
    # publish must not raise.
    def body():
        publish_todo_update({"tasks": [], "remaining": 0, "total": 0})

    contextvars.copy_context().run(body)


def test_drain_without_reset_returns_empty() -> None:
    def body():
        assert drain_todo_events() == []

    contextvars.copy_context().run(body)
```

> 把这两个用例的**第一版**(非 copy_context)删掉,只保留 copy_context 版。最终 `test_plan_todo_context.py` 新增三用例:`test_publish_then_drain`、`test_publish_without_reset_is_noop`(copy_context 版)、`test_drain_without_reset_returns_empty`(copy_context 版)。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plan_todo_context.py -v`
Expected: FAIL — `ImportError: cannot import name 'drain_todo_events'` (etc.) from `plan_todo_context`.

- [ ] **Step 3: Write minimal implementation** (append to `twinkle/agentserver/plan_todo_context.py`)

```python
TODO_EVENTS: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "twinkle_todo_events",
    default=None,
)


def reset_todo_events() -> None:
    """Arm a fresh per-request event bus (called at run_stream entry)."""
    TODO_EVENTS.set([])


def publish_todo_update(snapshot: dict) -> None:
    """Append a structured todo snapshot to the per-request bus.

    No-op when the bus is uninitialized (None) — e.g. when a todo tool is
    invoked directly outside of run_stream (tests, ad-hoc calls). This keeps
    the tool's return value (markdown string for the model) unchanged.
    """
    evs = TODO_EVENTS.get()
    if evs is None:
        return
    evs.append(snapshot)


def drain_todo_events() -> list[dict]:
    """Return and clear pending todo snapshots. Empty list if no bus."""
    evs = TODO_EVENTS.get()
    if not evs:
        return []
    out = list(evs)
    evs.clear()
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_plan_todo_context.py -v`
Expected: PASS (5 passed: 2 existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/plan_todo_context.py tests/test_plan_todo_context.py
git commit -m "Phase 2: todo 事件总线 (ContextVar list + publish/drain)"
```

---

### Task 2: todo_tools.py — publish 快照

**Files:**
- Modify: `twinkle/agentserver/tools/todo_tools.py`
- Test: `tests/test_todo_tools.py`

**Interfaces:**
- Consumes: `publish_todo_update`(Task 1)、`TodoTask`(现有)。
- Produces: `todo_create`/`todo_complete` 在成功路径 publish `{"tasks": [...], "remaining": n, "total": m}`;`tasks` 元素 `{"idx","title","status","result"}`。

- [ ] **Step 1: Write the failing tests** (append to `tests/test_todo_tools.py`)

Add `reset_todo_events` / `drain_todo_events` to the import line at top:

```python
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    reset_todo_events,
)
```

Append tests:

```python
def test_create_publishes_snapshot() -> None:
    _set_sid("pub-1")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["a", "b"]}))
    evs = drain_todo_events()
    assert len(evs) == 1
    snap = evs[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 2
    assert [t["idx"] for t in snap["tasks"]] == [1, 2]
    assert all(t["status"] == "waiting" for t in snap["tasks"])
    assert snap["tasks"][0]["title"] == "a"


def test_complete_publishes_snapshot() -> None:
    _set_sid("pub-2")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["x", "y"]}))
    drain_todo_events()  # clear create's snapshot
    asyncio.run(todo_complete.invoke({"idx": 1, "result": "ok"}))
    evs = drain_todo_events()
    assert len(evs) == 1
    snap = evs[0]
    assert snap["total"] == 2
    assert snap["remaining"] == 1
    assert snap["tasks"][0]["status"] == "completed"
    assert snap["tasks"][0]["result"] == "ok"


def test_list_does_not_publish() -> None:
    _set_sid("pub-3")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["a"]}))
    drain_todo_events()
    asyncio.run(todo_list.invoke({}))
    assert drain_todo_events() == []


def test_error_path_does_not_publish() -> None:
    _set_sid("pub-4")
    reset_todo_events()
    asyncio.run(todo_create.invoke({"tasks": ["first"]}))
    drain_todo_events()
    # second create fails (already exists) — must NOT publish
    asyncio.run(todo_create.invoke({"tasks": ["second"]}))
    assert drain_todo_events() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: FAIL — new tests: `drain_todo_events()` returns `[]` because `todo_create` doesn't publish yet.

- [ ] **Step 3: Write minimal implementation** (modify `twinkle/agentserver/tools/todo_tools.py`)

Add `publish_todo_update` to the import:

```python
from twinkle.agentserver.plan_todo_context import (
    get_plan_todo_session_id,
    publish_todo_update,
)
```

Add a `_snapshot` helper after `_append_list`:

```python
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
```

In `todo_create` success branch, publish before returning:

```python
@tool
async def todo_create(tasks: list[str]) -> str:
    """Create a list of todo tasks to plan and track multi-step work. Do not use for single-step simple requests. Pass a list of task descriptions; fails if a todo list already exists for this session.
    """
    sid = get_plan_todo_session_id()
    try:
        created = await _store.create(sid, tasks)
        publish_todo_update(_snapshot(created))
        return _append_list(f"Created {len(created)} todo tasks.", created)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)
```

In `todo_complete` success branch:

```python
@tool
async def todo_complete(idx: int, result: str = "") -> str:
    """Mark a todo task as completed and save a brief result. Pass the 1-based idx and an optional short result string.
    """
    sid = get_plan_todo_session_id()
    try:
        tasks = await _store.complete(sid, idx, result)
        publish_todo_update(_snapshot(tasks))
        return _append_list(f"Task {idx} marked as completed.", tasks)
    except TodoError as exc:
        current = await _store.list_tasks(sid)
        return _append_list(f"Error: {exc}", current)
```

`todo_list` unchanged (no publish). Error branches unchanged (no publish).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_tools.py -v`
Expected: PASS (11 passed: 7 existing + 4 new). Existing tests still pass because `publish_todo_update` no-ops when bus is `None` (those tests don't call `reset_todo_events()`).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/tools/todo_tools.py tests/test_todo_tools.py
git commit -m "Phase 2: todo 工具 publish 结构化快照 (create/complete 成功路径)"
```

---

### Task 3: gateway wire — EventType + MessageHandler 分支 + e2a 注释

**Files:**
- Modify: `twinkle/schema/message.py`
- Modify: `twinkle/e2a/models.py`(注释)
- Modify: `twinkle/gateway/message_handler.py`
- Test: `tests/test_message_handler.py`(新建)

**Interfaces:**
- Consumes: `E2AResponse.response_kind`(现有 str 字段)、`Message.payload`(现有 dict 字段)。
- Produces: `EventType.TODO_UPDATE`;`MessageHandler._process_stream` 对 `response_kind == "e2a.todo_update"` 的 E2AResponse 产出 `Message(event_type=TODO_UPDATE, payload=resp.body, content="")`。

- [ ] **Step 1: Write the failing test** (new file `tests/test_message_handler.py`)

```python
import asyncio

from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.gateway.message_handler import MessageHandler
from twinkle.schema.message import EventType, Message


class _FakeAgentClient:
    """Yields a scripted list of E2AResponse frames for one request."""

    def __init__(self, frames):
        self._frames = frames

    async def send_request_stream(self, envelope: E2AEnvelope):
        for f in self._frames:
            yield f


def _envelope(rid="r1", session_id="s1") -> E2AEnvelope:
    return E2AEnvelope(
        request_id=rid, session_id=session_id, method="chat.send", params={"query": "q"}
    )


def test_todo_update_frame_becomes_todo_event() -> None:
    todo_body = {
        "tasks": [{"idx": 1, "title": "a", "status": "waiting", "result": ""}],
        "remaining": 1,
        "total": 1,
    }
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=False,
            status="in_progress", response_kind="e2a.todo_update", body=todo_body,
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="chat.send", params={"query": "q"})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.TODO_UPDATE
    assert out.payload == todo_body
    assert out.content == ""


def test_chunk_frame_still_becomes_chat_delta() -> None:
    frames = [
        E2AResponse(
            request_id="r1", sequence=0, is_final=False,
            status="in_progress", response_kind="e2a.chunk",
            body={"result": {"content": "hi"}},
        ),
    ]
    handler = MessageHandler(_FakeAgentClient(frames))

    async def run():
        await handler.handle_message(
            Message(id="r1", type="req", channel_id="web", session_id="s1",
                    method="chat.send", params={"query": "q"})
        )
        return await handler.dequeue_outbound()

    out = asyncio.run(run())
    assert out.event_type == EventType.CHAT_DELTA
    assert out.content == "hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_message_handler.py -v`
Expected: FAIL — `EventType.TODO_UPDATE` does not exist (AttributeError), and/or MessageHandler emits CHAT_DELTA for the todo_update frame (because it only checks `is_final`, not `response_kind`).

- [ ] **Step 3: Write minimal implementation**

`twinkle/schema/message.py` — add the enum value:

```python
class EventType(str, Enum):
    CONNECTION_ACK = "connection.ack"
    CHAT_DELTA = "chat.delta"
    CHAT_FINAL = "chat.final"
    TODO_UPDATE = "todo.update"
```

`twinkle/e2a/models.py` — update the `response_kind` comment (line ~44):

```python
    response_kind: str = "e2a.chunk"  # e2a.chunk | e2a.complete | e2a.error | e2a.todo_update
```

`twinkle/gateway/message_handler.py` — replace the body of `_process_stream`'s `async for` loop. The current loop body is:

```python
        try:
            async for resp in self._agent_client.send_request_stream(envelope):
                content = (resp.body.get("result") or {}).get("content", "")
                event_type = EventType.CHAT_FINAL if resp.is_final else EventType.CHAT_DELTA
                out = Message(
                    id=msg.id,
                    type="event",
                    channel_id=msg.channel_id,
                    session_id=msg.session_id,
                    event_type=event_type,
                    content=content,
                )
                await self.enqueue_outbound(out)
```

Replace with a version that branches on `response_kind`:

```python
        try:
            async for resp in self._agent_client.send_request_stream(envelope):
                if resp.response_kind == "e2a.todo_update":
                    out = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=EventType.TODO_UPDATE,
                        payload=dict(resp.body),
                        content="",
                    )
                else:
                    content = (resp.body.get("result") or {}).get("content", "")
                    event_type = EventType.CHAT_FINAL if resp.is_final else EventType.CHAT_DELTA
                    out = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=event_type,
                        content=content,
                    )
                await self.enqueue_outbound(out)
```

> 注:`payload=dict(resp.body)`——`WebChannel.send` 已把 `Message.payload` 放进出站 frame 的 `payload` 字段(`web_channel.py:76`),前端可直接读 `frame.payload`。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_message_handler.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add twinkle/schema/message.py twinkle/e2a/models.py twinkle/gateway/message_handler.py tests/test_message_handler.py
git commit -m "Phase 2: gateway 按 response_kind 分支 → todo.update 浏览器事件"
```

---

### Task 4: agent_loop.py — reset + drain → yield e2a.todo_update

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `reset_todo_events` / `drain_todo_events`(Task 1)。
- Produces:`run_stream` 入口 `reset_todo_events()`;每次 `tools.execute` 后 drain,yield `E2AResponse(response_kind="e2a.todo_update", body=snapshot)`。

- [ ] **Step 1: Write the failing test** (append to `tests/test_agent_loop.py`)

```python
def test_todo_update_frame_emitted_on_create() -> None:
    """run_stream yields an e2a.todo_update frame after todo_create executes,
    carrying the structured snapshot (not just the markdown tool string)."""
    from twinkle.agentserver.tools import tool_manager

    store = SessionStore()
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "tc1", "type": "function",
                              "function": {"name": "todo_create",
                                           "arguments": '{"tasks": ["one", "two"]}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, tool_manager(), LongTermMemory())

    async def run():
        return [f async for f in loop.run_stream(_env("plan", session_id="s-upd"))]

    frames = asyncio.run(run())
    todo_frames = [f for f in frames if f.response_kind == "e2a.todo_update"]
    assert len(todo_frames) == 1
    body = todo_frames[0].body
    assert [t["idx"] for t in body["tasks"]] == [1, 2]
    assert body["remaining"] == 2
    assert body["total"] == 2
    assert body["tasks"][0]["title"] == "one"
    # the todo_update frame is not final and precedes the final complete
    assert not todo_frames[0].is_final
    assert frames[-1].response_kind == "e2a.complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_loop.py::test_todo_update_frame_emitted_on_create -v`
Expected: FAIL — no `e2a.todo_update` frame yielded (frames only contains chunks/complete).

- [ ] **Step 3: Write minimal implementation** (modify `twinkle/agentserver/agent_loop.py`)

Add `reset_todo_events` / `drain_todo_events` to the import from `plan_todo_context`:

```python
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    reset_todo_events,
)
```

In `run_stream`, after `PLAN_TODO_SESSION_ID.set(...)` (line ~47), add the bus reset:

```python
        session_id = envelope.session_id
        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
```

In the tool-execution `for tc in tcs:` block, after `result = await self._tools.execute(name, args)` and before `self._store.append(...)`, drain and yield:

```python
                            result = await self._tools.execute(name, args)
                            for ev in drain_todo_events():
                                yield E2AResponse(
                                    request_id=envelope.request_id,
                                    sequence=seq,
                                    is_final=False,
                                    status="in_progress",
                                    response_kind="e2a.todo_update",
                                    body=ev,
                                )
                                seq += 1
                            self._store.append(
                                session_id,
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                },
                            )
```

The rest of `run_stream` is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: PASS (all existing + the new `test_todo_update_frame_emitted_on_create`).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/agent_loop.py tests/test_agent_loop.py
git commit -m "Phase 2: agent_loop drain todo 事件 → yield e2a.todo_update frame"
```

---

### Task 5: 前端 — webClient + App.vue TodoPanel

**Files:**
- Modify: `web/src/services/webClient.ts`
- Modify: `web/src/App.vue`

> 前端无单测;实现后手动验证(Step 5)。

- [ ] **Step 1: Modify `webClient.ts`**

Replace the top type/handler section (lines 1-13) with a version adding `TodoUpdateHandler`:

```typescript
// Minimal WebSocket client: sends {type:req,id,method,params}, correlates
// streamed chat.delta / chat.final events by request_id, and surfaces
// todo.update events (structured todo snapshots) for the side panel.

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
```

Add a private handler field (after `private onFinal`):

```typescript
  private onTodoUpdate: TodoUpdateHandler = () => {}
```

In `handle`, extend the `frame.type === 'event'` branch (lines 39-44):

```typescript
    if (frame.type === 'event') {
      const rid = frame.request_id
      const content = frame.payload?.content ?? ''
      if (frame.event === 'chat.delta') this.onDelta(content, rid)
      else if (frame.event === 'chat.final') this.onFinal(content, rid)
      else if (frame.event === 'todo.update') this.onTodoUpdate(frame.payload ?? { tasks: [], remaining: 0, total: 0 }, rid)
    }
```

Replace `setHandlers` (lines 47-50) with a three-arg version:

```typescript
  setHandlers(onDelta: DeltaHandler, onFinal: FinalHandler, onTodoUpdate: TodoUpdateHandler): void {
    this.onDelta = onDelta
    this.onFinal = onFinal
    this.onTodoUpdate = onTodoUpdate
  }
```

- [ ] **Step 2: Modify `App.vue`**

In `<script setup>`, add the todo state ref and wire the third handler. After the `const client = new WebClient()` / `let currentId` lines, the `onMounted` block becomes:

```typescript
interface TodoState { tasks: TodoTask[]; remaining: number; total: number }
const todo = ref<TodoState | null>(null)

onMounted(() => {
  client.connect(() => { connected.value = true })
  client.setHandlers(
    (delta, rid) => {
      if (rid !== currentId) return
      const last = msgs.value[msgs.value.length - 1]
      if (last && last.role === 'assistant') last.content += delta
      else msgs.value.push({ role: 'assistant', content: delta })
      scrollDown()
    },
    (text, rid) => {
      if (rid !== currentId) return
      const last = msgs.value[msgs.value.length - 1]
      if (!last || last.role !== 'assistant') msgs.value.push({ role: 'assistant', content: text })
      else if (!last.content) last.content = text
      scrollDown()
    },
    (t) => { todo.value = t },
  )
})
```

Add `TodoTask` to the imports (line 3):

```typescript
import { WebClient, type TodoTask } from './services/webClient'
```

In `<template>`, wrap the existing `.chat` content in a two-column layout. Replace the `<template>` block with:

```html
<template>
  <div class="app">
    <div class="chat">
      <header>
        <span class="title">✨ Twinkle</span>
        <span class="subtitle">Phase 0 Echo</span>
        <span class="status" :class="{ on: connected }">{{ connected ? '已连接' : '连接中…' }}</span>
      </header>
      <ul ref="logEl" class="log">
        <li v-for="(m, i) in msgs" :key="i" :class="['row', m.role]">
          <div class="bubble">{{ m.content }}</div>
        </li>
      </ul>
      <footer>
        <input
          v-model="input"
          @keyup.enter="send"
          :disabled="!connected"
          placeholder="说点什么…"
        />
        <button @click="send" :disabled="!connected">发送</button>
      </footer>
    </div>
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
  </div>
</template>
```

Add computed helpers in `<script setup>` (after `scrollDown` or near the refs):

```typescript
import { ref, onMounted, nextTick, computed } from 'vue'

const completedCount = computed(() =>
  todo.value ? todo.value.tasks.filter((t) => t.status === 'completed').length : 0,
)

function box(status: TodoTask['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '◐'
  return '○'
}
```

Update `<style>`: change the root `.chat` rule to be a child of `.app`, and add `.app` + panel styles. Replace the existing `.chat { ... }` block (lines ~77-85) with:

```css
.app {
  display: flex;
  height: 100%;
  max-width: 1040px;
  margin: 0 auto;
}
.chat {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: #1e293b;
  background: #f8fafc;
}
.todo-panel {
  width: 280px;
  flex: 0 0 280px;
  border-left: 1px solid #e2e8f0;
  background: #fff;
  display: flex;
  flex-direction: column;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (max-width: 640px) {
  .app { flex-direction: column; max-width: 100%; }
  .todo-panel { width: 100%; flex: 0 0 auto; border-left: 0; border-top: 1px solid #e2e8f0; max-height: 40%; }
}
.todo-head {
  display: flex;
  justify-content: space-between;
  padding: .9rem 1rem;
  border-bottom: 1px solid #e2e8f0;
  font-weight: 600;
}
.todo-count { color: #6366f1; }
.todo-list { list-style: none; margin: 0; padding: .5rem; overflow-y: auto; flex: 1; }
.todo-item { display: flex; align-items: baseline; gap: .35rem; padding: .35rem .25rem; font-size: .9rem; }
.todo-item.completed .todo-title { text-decoration: line-through; color: #94a3b8; }
.todo-box { width: 1.1em; text-align: center; color: #4f46e5; }
.todo-item.completed .todo-box { color: #10b981; }
.todo-result { color: #64748b; font-size: .8rem; }
.todo-empty { padding: 1rem; color: #94a3b8; font-size: .85rem; }
```

- [ ] **Step 3: Type-check / build the frontend**

Run: `cd web && npm run build` (or `npm run dev` and check for compile errors in the terminal)
Expected: build succeeds (or dev server compiles with no errors). Fix any TS errors before committing.

- [ ] **Step 4: Manual verify (smoke)** — start backend + frontend, send a query that triggers `todo_create`, confirm the side panel renders the list with `0/N` progress, then `todo_complete` flips a row to ✓ and bumps the count. If you can't drive the real LLM (no API key), at least confirm `npm run build` passes and the panel renders "暂无任务" at startup.

- [ ] **Step 5: Commit**

```bash
git add web/src/services/webClient.ts web/src/App.vue
git commit -m "Phase 2: 前端 todo 侧边面板 (webClient todo.update + App.vue 两栏布局)"
```

---

### Task 6: Full suite + e2e smoke

**Files:** 无(仅跑测试 + 冒烟)

- [ ] **Step 1: Run the full backend suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — all prior + new `test_plan_todo_context` (3 new) / `test_todo_tools` (4 new) / `test_agent_loop` (1 new) / `test_message_handler` (2 new).

- [ ] **Step 2: Frontend build**

Run: `cd web && npm run build`
Expected: succeeds with no TS errors.

- [ ] **Step 3: Production wiring import smoke**

Run: `python -c "from twinkle.agentserver.server import agent_loop; l = agent_loop(); print(sorted(t.card.name for t in l._tools.list()))"`
Expected: `['command_exec', 'todo_complete', 'todo_create', 'todo_list', 'web_fetch', 'web_search']` (unchanged — UI work adds no tools).

- [ ] **Step 4: Commit if any fixes from Step 1/2**

```bash
git add -A && git commit -m "Phase 2: todo progress UI — test suite + build green"
```

> 若全绿无改动可跳过(工作树已 clean)。

---

## Self-Review(已执行)

- **Spec coverage**:spec §1 事件总线 → Task 1;§1 publish 快照 → Task 2;§2 MessageHandler 分支 + EventType + e2a 注释 → Task 3;§2 agent_loop drain → Task 4;§2 前端面板 → Task 5;§3 数据流 → Task 4 注释 + Task 4/5 测试覆盖;§4 错误处理(bus None no-op、错误路径不 publish、前端可选链)→ Task 1(no-op)+ Task 2(error 不 publish)+ Task 5(可选链 `todo.value?.tasks`);§5 测试 → Task 1-4 各自 + Task 6 全量。`todo_clear`/rail/前端单测明确不做。
- **Placeholder scan**:无 TBD;每步有完整代码与断言。Task 5 无单测(前端无基建,spec 明确)。
- **Type consistency**:`reset_todo_events`/`publish_todo_update`/`drain_todo_events` Task 1 定义,Task 2/4 调用名一致;`_snapshot` 返回 `{tasks, remaining, total}` 在 Task 2 定义、Task 4 body 断言、Task 5 `TodoState` 接口一致;`TodoTask` 字段(idx/title/status/result)Task 2/5 一致;`EventType.TODO_UPDATE` Task 3 定义、Task 3 测试 + Task 4 间接使用一致。
- **关键风险已注**:Task 1 的「未 reset 时 no-op」用 `contextvars.copy_context()` 隔离测试,避免 ContextVar 跨测试污染(已在 Step 1 注释里说明并给出最终 copy_context 版)。Task 2 现有用例不挂(依赖 Task 1 no-op)。Task 3 fake AgentClient 匹配真实 `send_request_stream` 签名(async gen)。Task 4 drain 在 execute 后、store.append 前 yield。
