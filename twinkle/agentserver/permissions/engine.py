"""PermissionEngine — 通道门 + 审计 + 委托 policy。

check() 序:enabled=false 或 channel 不在 enabled_channels → ALLOW 透传(passthrough,
不审计级别);否则交 policy.check + 写 ToolPermissionLog。persist_allow_always 委托 policy。
"""
from __future__ import annotations

from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.models import (
    PermissionDecision, PermissionLevel, ToolPermissionLogEntry)
from twinkle.agentserver.permissions.policy import PermissionPolicy


class PermissionEngine:
    def __init__(
        self,
        policy: PermissionPolicy,
        audit: ToolPermissionLog,
        enabled: bool,
        enabled_channels: set[str],
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._enabled = enabled
        self._channels = set(enabled_channels)

    def check(
        self,
        tool: str,
        args: dict,
        channel: str,
        session_id: str | None,
        request_id: str | None,
    ) -> PermissionDecision:
        if not self._enabled or channel not in self._channels:
            return PermissionDecision(level=PermissionLevel.ALLOW, reason="disabled or channel not gated",
                                      source="passthrough")
        decision = self._policy.check(tool, args)
        self._audit.log(ToolPermissionLogEntry(
            tool=tool, decision=decision.level, source=decision.source,
            rule_id=decision.rule_id, reason=decision.reason, user_decision=None,
            channel=channel, session_id=session_id, request_id=request_id))
        return decision

    async def persist_allow_always(self, decision_data: dict) -> None:
        await self._policy.persist_allow_always(decision_data)
        # 二次审计行:user_decision
        self._audit.log(ToolPermissionLogEntry(
            tool=decision_data.get("tool", ""), decision="ask", source="override",
            reason="allow_always persisted", user_decision="allow_always",
            channel="", session_id=decision_data.get("session_id"),
            request_id=decision_data.get("request_id")))
