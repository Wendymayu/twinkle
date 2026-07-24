from twinkle.agentserver.permissions.models import (
    PermissionLevel, PermissionDecision, ToolPermissionLogEntry)


def test_permission_levels():
    assert PermissionLevel.ALLOW == "allow"
    assert PermissionLevel.ASK == "ask"
    assert PermissionLevel.DENY == "deny"


def test_decision_carries_fields():
    d = PermissionDecision(level="deny", reason="rm -rf", source="rule", rule_id="rm-rf",
                           deny_message="[ERROR]: command rejected for safety (rm -rf).")
    assert d.level == "deny"
    assert d.source == "rule"
    assert d.deny_message.startswith("[ERROR]")


def test_log_entry_round_trip():
    e = ToolPermissionLogEntry(tool="command_exec", decision="deny", source="rule",
                               rule_id="rm-rf", reason="blocked", user_decision=None,
                               channel="web", session_id="s1", request_id="r1")
    d = e.to_dict()
    assert d["tool"] == "command_exec" and d["decision"] == "deny" and "ts" in d
