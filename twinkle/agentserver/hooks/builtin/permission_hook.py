"""PermissionHook — before_tool_call 权限拦截。

ALLOW → no-op(工具正常执行);DENY → request_force_finish(deny_msg 变 tool_result
回灌,走 @hook 短路);ASK → raise HookInterrupt(ask_payload),由 _inner_run_stream
的 except 捕获后挂起/恢复(spec §7)。已批 tool_call_id  bypass 避免恢复后重调再问。

bypass 分支还负责 allow_always 持久化——审批决策通过 ctx.extra 传入,
PermissionHook 自行决定是否持久化,不再依赖 AgentLoop 持有 engine。
"""
from __future__ import annotations

import uuid

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookInterrupt, ToolCallInputs
from twinkle.agentserver.permission_context import get_permission_channel
from twinkle.agentserver.permissions.engine import PermissionEngine


class PermissionHook(AgentHook):
    """执行 PermissionEngine 决策的 before_tool_call hook。

    按 decision level 分派：
      - ALLOW → no-op（tool 正常执行）
      - DENY  → request_force_finish(deny_message)；@hook 装饰器
        短路，deny message 成为 tool_result
      - ASK   → raise HookInterrupt(ask_payload)；_inner_run_stream 的
        except HookInterrupt 挂起 run 等待人工审批

    已批 tool_call_id bypass 避免恢复后重问 — 一旦某 tool call
    被批准（ASK→resume），其 id 记入 ctx.extra["_approved_tool_call_ids"]，
    重入时跳过。

    bypass 分支还负责持久化 allow_always 覆盖（当审批决策为
    "allow_always" 时） — 决策由 _inner_run_stream 通过
    ctx.extra["_approval_decision"] 传入，故 PermissionHook 自行
    处理持久化，AgentLoop 无需持有 engine 引用。
    """

    priority = 100  # 先于 LoggingHook 等 before_tool_call hook

    def __init__(self, engine: PermissionEngine) -> None:
        self._engine = engine

    async def before_tool_call(self, ctx: HookContext) -> None:
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        approved_ids = ctx.extra.get("_approved_tool_call_ids", set())
        if inputs.tool_call_id in approved_ids:
            # Bypass：本 tool_call 在同一 run 内已被批准。
            # 若决策为 "allow_always" 则持久化 allow_always。
            if ctx.extra.get("_approval_decision") == "allow_always":
                await self._engine.persist_allow_always({
                    "tool": inputs.name, "args": inputs.args,
                    "tool_call_id": inputs.tool_call_id,
                    "session_id": ctx.session_id, "request_id": ctx.request_id,
                })
            return  # 本 run 已批准(ASK 恢复后重调用),放行
        decision = self._engine.check(
            tool=inputs.name, args=inputs.args,
            channel=get_permission_channel(),
            session_id=ctx.session_id, request_id=ctx.request_id)
        if decision.level == "deny":
            ctx.request_force_finish(decision.deny_message)
        elif decision.level == "ask":
            raise HookInterrupt(
                message="approval required",
                data={
                    "approval_id": str(uuid.uuid4()),
                    "tool": inputs.name, "args": inputs.args,
                    "tool_call_id": inputs.tool_call_id, "reason": decision.reason,
                    "request_id": ctx.request_id, "session_id": ctx.session_id,
                })
        # allow → no-op
