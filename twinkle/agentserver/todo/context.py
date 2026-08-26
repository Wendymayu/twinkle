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


TODO_EVENTS: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "twinkle_todo_events",
    default=None,
)


def reset_todo_events() -> None:
    """启用一个全新的 per-request event bus(在 run_stream 入口调用)。"""
    TODO_EVENTS.set([])


def append_todo_event(snapshot: dict) -> None:
    """向 per-request event buffer 追加一个结构化的 todo 快照。

    buffer 未初始化(None)时是 no-op —— 比如在 run_stream 之外
    直接调用 todo 工具(测试、临时调用)时。这保证
    工具的返回值(给模型的 markdown 字符串)不变。

    真正的发布发生在 agent_loop,由 flush_todo_events() yield
    e2a.todo_update 帧时进行。
    """
    todo_events = TODO_EVENTS.get()
    if todo_events is None:
        return
    todo_events.append(snapshot)


def flush_todo_events() -> list[dict]:
    """排空 per-request event buffer：返回待发快照并清空。

    返回累积的快照供调用方(agent_loop)yield
    为 e2a.todo_update 帧。buffer 未初始化或已空时返回空列表。
    """
    todo_events = TODO_EVENTS.get()
    if not todo_events:
        return []
    snapshots = list(todo_events)
    todo_events.clear()
    return snapshots
