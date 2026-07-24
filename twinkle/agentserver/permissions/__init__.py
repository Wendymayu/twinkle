"""permissions 包入口 — re-exports + permission_engine() builder。"""
from twinkle.agentserver.permissions.models import (
    PermissionDecision, PermissionLevel, ToolPermissionLogEntry)
from twinkle.agentserver.permissions.builtin_rules import COMMAND_DENY_PATTERNS, matches
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.approval_registry import (
    ApprovalRegistry, APPROVAL_REGISTRY)
from twinkle.agentserver.permissions.engine import PermissionEngine


def permission_engine() -> PermissionEngine:
    """从 config 构造一个 PermissionEngine(生产装配用)。"""
    from twinkle.config import (
        PERMISSIONS_ENABLED, PERMISSIONS_ENABLED_CHANNELS, PERMISSIONS_GLOBAL_DEFAULT,
        PERMISSIONS_TOOLS, PERMISSIONS_RULES, PERMISSION_OVERRIDES_FILE, PERMISSION_AUDIT_FILE)
    policy = PermissionPolicy(
        tools=dict(PERMISSIONS_TOOLS), rules=list(PERMISSIONS_RULES),
        approval_overrides={}, global_default=PERMISSIONS_GLOBAL_DEFAULT,
        overrides_file=PERMISSION_OVERRIDES_FILE)
    audit = ToolPermissionLog(PERMISSION_AUDIT_FILE)
    return PermissionEngine(policy=policy, audit=audit, enabled=PERMISSIONS_ENABLED,
                            enabled_channels=PERMISSIONS_ENABLED_CHANNELS)


__all__ = [
    "PermissionEngine", "PermissionPolicy", "ToolPermissionLog", "ToolPermissionLogEntry",
    "ApprovalRegistry", "APPROVAL_REGISTRY", "PermissionDecision", "PermissionLevel",
    "COMMAND_DENY_PATTERNS", "matches", "permission_engine",
]
