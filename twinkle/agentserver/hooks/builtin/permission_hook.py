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

    The bypass branch also persists allow_always overrides if the approval
    decision was "allow_always" — the decision is passed via
    ctx.extra["_approval_decision"] by _inner_run_stream, so PermissionHook
    handles its own persistence without AgentLoop needing an engine reference.
    """

    priority = 100  # 先于 LoggingHook 等 before_tool_call hook

    def __init__(self, engine) -> None:
        self._engine = engine

    async def before_tool_call(self, ctx: HookContext) -> None:
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        approved_ids = ctx.extra.get("_approved_tool_call_ids", set())
        if inputs.tool_call_id in approved_ids:
            # Bypass: this tool_call was approved in the same run.
            # Persist allow_always if the decision was "allow_always".
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
