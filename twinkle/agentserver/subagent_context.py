"""Subagent ContextVar bridge — lets the parameter-less spawn_subagent tool
find the current executor + parent session/request id at runtime.

Set by SubagentContextHook.before_invoke on the PARENT loop only (the child
has no spawn_subagent, so it never reads these). Mirrors plan_todo_context.py.
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from twinkle.agentserver.tools.subagent_executor import SubagentExecutor

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
