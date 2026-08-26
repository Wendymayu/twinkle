"""权限系统的纯数据类型。对齐 jiuwenswarm permissions/models.py 的子集。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


class PermissionLevel:
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionDecision:
    level: str                       # "allow" | "ask" | "deny"
    reason: str = ""
    source: str = ""                 # "tier" | "rule" | "override" | "passthrough"
    rule_id: str | None = None
    deny_message: str = ""


@dataclass
class ToolPermissionLogEntry:
    tool: str
    decision: str
    source: str
    rule_id: str | None = None
    reason: str = ""
    user_decision: str | None = None
    channel: str = "web"
    session_id: str | None = None
    request_id: str | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolExecutionAuditEntry:
    """Always-on 工具执行审计条目(与 permissions.enabled 解耦)。

    outcome: success | denied(权限拒绝) | <ToolError.kind>(denied/validation/failed) | error
    """
    tool: str
    outcome: str
    args: str = ""        # JSON 串,已截断
    result: str = ""     # 已截断(success 才有)
    error: str = ""      # 异常消息,已截断(error/exception 才有)
    tool_call_id: str = ""
    session_id: str | None = None
    request_id: str | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
