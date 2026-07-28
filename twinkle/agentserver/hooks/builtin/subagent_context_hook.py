"""SubagentContextHook — sets the subagent ContextVar bridge at run_stream entry.

Registered on the PARENT loop only. before_invoke fires once per run_stream
(same entry point where run_stream itself sets PLAN_TODO_SESSION_ID etc.).
The child loop does NOT register this hook — it has no spawn_subagent, so the
ContextVars would never be read there.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.subagent_context import (
    SUBAGENT_EXECUTOR,
    SUBAGENT_PARENT_REQUEST_ID,
    SUBAGENT_PARENT_SESSION_ID,
)

if TYPE_CHECKING:
    from twinkle.agentserver.tools.subagent_executor import SubagentExecutor


class SubagentContextHook(AgentHook):
    priority = 50

    def __init__(self, executor: "SubagentExecutor") -> None:
        self._executor = executor

    async def before_invoke(self, ctx: HookContext) -> None:
        SUBAGENT_EXECUTOR.set(self._executor)
        SUBAGENT_PARENT_SESSION_ID.set(ctx.session_id)
        SUBAGENT_PARENT_REQUEST_ID.set(ctx.request_id)
