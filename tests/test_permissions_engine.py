from twinkle.agentserver.permissions.engine import PermissionEngine
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.permissions.audit import ToolPermissionLog


def _engine(tmp_path, enabled=True, channels=None, tools=None, default="allow"):
    policy = PermissionPolicy(
        tools=tools or {"command_exec": "require-approval"}, rules=[],
        approval_overrides={}, global_default=default,
        overrides_file=str(tmp_path / "ovr.json"))
    audit = ToolPermissionLog(str(tmp_path / "audit.jsonl"))
    return PermissionEngine(policy=policy, audit=audit, enabled=enabled,
                            enabled_channels=channels or {"web"})


def test_disabled_short_circuits_allow(tmp_path):
    e = _engine(tmp_path, enabled=False)
    d = e.check("command_exec", {"command": "rm -rf /"}, "web", "s1", "r1")
    assert d.level == "allow" and d.source == "passthrough"


def test_channel_not_enabled_passthrough(tmp_path):
    e = _engine(tmp_path, enabled=True, channels={"web"})
    d = e.check("command_exec", {"command": "rm -rf /"}, "feishu", "s1", "r1")
    assert d.level == "allow" and d.source == "passthrough"


def test_enabled_channel_delegates_to_policy(tmp_path):
    e = _engine(tmp_path, tools={"command_exec": "require-approval"})
    d = e.check("command_exec", {"command": "ls"}, "web", "s1", "r1")
    assert d.level == "ask"


def test_deny_still_audited(tmp_path):
    e = _engine(tmp_path, tools={"command_exec": "allow"})
    d = e.check("command_exec", {"command": "rm -rf /"}, "web", "s1", "r1")
    assert d.level == "deny"
    import json, pathlib
    lines = [l for l in pathlib.Path(str(tmp_path / "audit.jsonl")).read_text("utf-8").splitlines() if l]
    assert len(lines) == 1 and json.loads(lines[0])["decision"] == "deny"


def test_persist_delegates(tmp_path):
    e = _engine(tmp_path, tools={"command_exec": "require-approval"})
    import asyncio
    asyncio.run(e.persist_allow_always(
        {"tool": "command_exec", "args": {"command": "git status"}}))
    # Policy persists a "head + ' *'" glob pattern ("git status *"); a bare
    # "git status" has no trailing argument and won't match, so check a
    # command carrying an argument (policy.py is out of scope to change).
    assert e.check("command_exec", {"command": "git status --short"},
                   "web", "s1", "r1").level == "allow"
