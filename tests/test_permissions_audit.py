import json
from pathlib import Path

from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.models import ToolPermissionLogEntry


def test_log_appends_jsonl(tmp_path):
    f = tmp_path / "audit.jsonl"
    log = ToolPermissionLog(str(f))
    log.log(ToolPermissionLogEntry(tool="command_exec", decision="deny", source="rule",
                                   rule_id="rm-rf", reason="blocked", channel="web",
                                   session_id="s1", request_id="r1"))
    log.log(ToolPermissionLogEntry(tool="command_exec", decision="ask", source="tier",
                                   user_decision="allow_always", channel="web",
                                   session_id="s1", request_id="r1"))
    lines = [json.loads(l) for l in f.read_text("utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["decision"] == "deny" and lines[1]["user_decision"] == "allow_always"
    assert "ts" in lines[0]


def test_log_makes_parent_dir(tmp_path):
    f = tmp_path / "nested" / "dir" / "audit.jsonl"
    ToolPermissionLog(str(f)).log(ToolPermissionLogEntry(
        tool="echo", decision="allow", source="tier"))
    assert f.is_file()


def test_log_is_fail_soft(tmp_path):
    # a bad path must not raise
    ToolPermissionLog("/nonexistent-root/x/audit.jsonl").log(ToolPermissionLogEntry(
        tool="echo", decision="allow", source="tier"))  # no exception
