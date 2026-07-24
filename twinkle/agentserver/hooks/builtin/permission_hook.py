"""PermissionHook — before_tool_call 权限拦截。

ALLOW → no-op(工具正常执行);DENY → request_force_finish(deny_msg 变 tool_result
回灌,走 @hook 短路);ASK → raise HookInterrupt(ask_payload),由 _inner_run_stream
的 except 捕获后挂起/恢复(spec §7)。已批 tool_call_id 走 bypass 避免恢复后重调再问。
"""
from __future__ import annotations

import uuid

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookInterrupt, ToolCallInputs
from twinkle.agentserver.permission_context import get_permission_channel


class PermissionHook(AgentHook):
    """before_tool_call hook enforcing PermissionEngine decisions.

    Dispatches by decision level:
      - ALLOW → no-op (tool executes normally)
      - DENY  → request_force_finish(deny_message); the @hook decorator
        short-circuits and the deny message becomes the tool_result
      - ASK   → raise HookInterrupt(ask_payload); _inner_run_stream's
        except HookInterrupt suspends the run awaiting human approval

    An approved tool_call_id bypass avoids re-asking on resume — once a
    tool call has been approved (ASK→resume), its id is recorded in
    ctx.extra["_approved_tool_call_ids"] and skipped on re-entry.
    """

    priority = 100  # 先于 LoggingHook 等 before_tool_call hook

    def __init__(self, engine) -> None:
        self._engine = engine

    async def before_tool_call(self, ctx: HookContext) -> None:
        inp: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        if inp.tool_call_id in ctx.extra.get("_approved_tool_call_ids", set()):
            return  # 本 run 已批准(ASK 恢复后重调用),放行
        decision = self._engine.check(
            tool=inp.name, args=inp.args,
            channel=get_permission_channel(),
            session_id=ctx.session_id, request_id=ctx.request_id)
        if decision.level == "deny":
            ctx.request_force_finish(decision.deny_message)
        elif decision.level == "ask":
            raise HookInterrupt(
                message="approval required",
                data={
                    "approval_id": str(uuid.uuid4()),
                    "tool": inp.name, "args": inp.args,
                    "tool_call_id": inp.tool_call_id, "reason": decision.reason,
                    "request_id": ctx.request_id, "session_id": ctx.session_id,
                })
        # allow → no-op
