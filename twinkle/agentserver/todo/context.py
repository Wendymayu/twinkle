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
    """Arm a fresh per-request event bus (called at run_stream entry)."""
    TODO_EVENTS.set([])


def append_todo_event(snapshot: dict) -> None:
    """Append a structured todo snapshot to the per-request event buffer.

    No-op when the buffer is uninitialized (None) — e.g. when a todo tool is
    invoked directly outside of run_stream (tests, ad-hoc calls). This keeps
    the tool's return value (markdown string for the model) unchanged.

    The actual publish happens in agent_loop when flush_todo_events() yields
    e2a.todo_update frames.
    """
    todo_events = TODO_EVENTS.get()
    if todo_events is None:
        return
    todo_events.append(snapshot)


def flush_todo_events() -> list[dict]:
    """Flush the per-request event buffer: return pending snapshots and clear.

    Returns the accumulated snapshots for the caller (agent_loop) to yield
    as e2a.todo_update frames. Empty list when the buffer is uninitialized
    or already empty.
    """
    todo_events = TODO_EVENTS.get()
    if not todo_events:
        return []
    snapshots = list(todo_events)
    todo_events.clear()
    return snapshots
