"""LoggingHook — 第一个具体的 AgentHook 示例。

通过标准 logging 模块记录 LLM 与 tool call 事件。
Priority=10 确保它在安全/功能 hook（85-100）之后、
可观测 span 管理（0）之前运行。
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.logging")


class LoggingHook(AgentHook):
    """通过标准 logging 模块记录 LLM 调用与 tool 执行。"""

    priority = 10  # 在安全（85-100）之后，可观测（0）之前

    async def before_model_call(self, ctx: HookContext) -> None:
        log.info("LLM call starting, session=%s", ctx.session_id)

    async def after_model_call(self, ctx: HookContext) -> None:
        log.info("LLM call finished, session=%s", ctx.session_id)

    async def before_tool_call(self, ctx: HookContext) -> None:
        log.info("tool %s starting, args=%s", ctx.inputs.name, ctx.inputs.args)

    async def after_tool_call(self, ctx: HookContext) -> None:
        log.info("tool %s finished, session=%s", ctx.session_id)
