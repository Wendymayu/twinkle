"""SubagentContextHook — sets the subagent ContextVar bridge at run_stream entry.

Auto-wired by build_agent_loop (which builds the executor and passes it here —
mirroring jiuwenswarm's adapter binding the executor onto its stream rail).
before_invoke fires once per run_stream (same entry point where run_stream
itself sets PLAN_TODO_SESSION_ID etc.) and sets the executor + parent
session/request id into the ContextVars that the parameter-less spawn_subagent
tool reads at runtime. Registered on the PARENT loop only; the child has no
spawn_subagent so it never reads these.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.tools.builtin.subagent.context import (
    SUBAGENT_EXECUTOR,
    SUBAGENT_PARENT_REQUEST_ID,
    SUBAGENT_PARENT_SESSION_ID,
)

if TYPE_CHECKING:
    from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor


class SubagentContextHook(AgentHook):
    priority = 50

    def __init__(self, executor: "SubagentExecutor") -> None:
        self._executor = executor

    async def before_invoke(self, ctx: HookContext) -> None:
        SUBAGENT_EXECUTOR.set(self._executor)
        SUBAGENT_PARENT_SESSION_ID.set(ctx.session_id)
        SUBAGENT_PARENT_REQUEST_ID.set(ctx.request_id)
