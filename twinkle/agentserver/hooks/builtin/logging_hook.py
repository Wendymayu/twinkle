"""LoggingHook — first concrete AgentHook demonstration.

Records LLM and tool call events via the standard logging module.
Priority=10 ensures it runs after security/functional hooks (85-100)
but before observability span management (0).
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook

log = logging.getLogger("twinkle.hooks.logging")


class LoggingHook(AgentHook):
    """Log LLM calls and tool executions via the standard logging module."""

    priority = 10  # After security (85-100), before observability (0)

    async def before_model_call(self, ctx) -> None:
        log.info("LLM call starting, session=%s", ctx.session_id)

    async def after_model_call(self, ctx) -> None:
        log.info("LLM call finished, session=%s", ctx.session_id)

    async def before_tool_call(self, ctx) -> None:
        log.info("tool %s starting, args=%s", ctx.inputs.name, ctx.inputs.args)

    async def after_tool_call(self, ctx) -> None:
        log.info("tool %s finished, session=%s", ctx.session_id)
