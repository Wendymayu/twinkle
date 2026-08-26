"""Subagent ContextVar 桥接 —— 让 spawn_subagent 工具能在运行时
找到当前 executor + 父 session/request id。

仅由 PARENT loop 上的 SubagentContextHook.before_invoke 设置(子 agent
没有 spawn_subagent,故永不读取这些)。对照 plan_todo_context.py。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor

SUBAGENT_EXECUTOR: contextvars.ContextVar["SubagentExecutor | None"] = contextvars.ContextVar(
    "twinkle_subagent_executor", default=None
)
SUBAGENT_PARENT_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "twinkle_subagent_parent_session_id", default=None
)
SUBAGENT_PARENT_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "twinkle_subagent_parent_request_id", default=None
)


def get_subagent_executor() -> "SubagentExecutor | None":
    return SUBAGENT_EXECUTOR.get()


def get_subagent_parent_session_id() -> str | None:
    return SUBAGENT_PARENT_SESSION_ID.get()


def get_subagent_parent_request_id() -> str | None:
    return SUBAGENT_PARENT_REQUEST_ID.get()
