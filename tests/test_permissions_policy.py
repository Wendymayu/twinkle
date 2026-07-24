from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.permissions.models import PermissionLevel


def _policy(tmp_path, tools=None, rules=None, overrides=None, default="allow"):
    return PermissionPolicy(
        tools=tools or {}, rules=rules or [], approval_overrides=overrides or {},
        global_default=default, overrides_file=str(tmp_path / "ovr.json"))


def test_tier_allow(tmp_path):
    p = _policy(tmp_path, tools={"echo": "allow"})
    d = p.check("echo", {})
    assert d.level == "allow" and d.source == "tier"


def test_tier_require_approval(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "require-approval"})
    d = p.check("command_exec", {"command": "ls"})
    assert d.level == "ask"


def test_deny_rule_command_exec_blocklist(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "allow"})
    d = p.check("command_exec", {"command": "rm -rf /"})
    assert d.level == "deny" and d.source == "rule"


def test_user_deny_rule_matches_args(tmp_path):
    p = _policy(tmp_path, tools={"echo": "allow"},
                 rules=[{"id": "no-foo", "tool": "echo", "pattern": "foo", "reason": "no foo"}])
    d = p.check("echo", {"text": "say foo bar"})
    assert d.level == "deny" and d.rule_id == "no-foo"


def test_global_default_for_unconfigured(tmp_path):
    p = _policy(tmp_path, default="ask")
    d = p.check("mystery_tool", {})
    assert d.level == "ask"


def test_allow_always_override_shell_head_wildcard(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "require-approval"},
                 overrides={"command_exec": ["git *"]})
    assert p.check("command_exec", {"command": "git status"}).level == "allow"
    # not blessed
    assert p.check("command_exec", {"command": "rm -rf x"}).level == "deny"
    assert p.check("command_exec", {"command": "npm install"}).level == "ask"


def test_allow_always_override_non_shell(tmp_path):
    p = _policy(tmp_path, tools={"web_fetch": "require-approval"},
                 overrides={"web_fetch": "allow"})
    assert p.check("web_fetch", {"url": "http://x"}).level == "allow"


def test_persist_allow_always_shell_writes_two_token_pattern(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "require-approval"})
    import asyncio
    asyncio.run(p.persist_allow_always(
        {"tool": "command_exec", "args": {"command": "npm run build"}}))
    d = p.check("command_exec", {"command": "npm run build"})
    assert d.level == "allow"  # override now blesses it
    # safety: a single-token-only blessing must NOT bless npm install -g
    assert p.check("command_exec", {"command": "npm install -g pkg"}).level == "ask"


def test_persist_allow_always_non_shell(tmp_path):
    p = _policy(tmp_path, tools={"web_fetch": "require-approval"})
    import asyncio
    asyncio.run(p.persist_allow_always(
        {"tool": "web_fetch", "args": {"url": "http://x"}}))
    assert p.check("web_fetch", {"url": "http://y"}).level == "allow"


def test_override_does_not_bless_metacharacter_chain(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "require-approval"},
                 overrides={"command_exec": ["npm run *"]})
    # chained dangerous command must NOT be blessed — falls through to deny
    assert p.check("command_exec", {"command": "npm run build && rm -rf /"}).level == "deny"
    # but the plain blessed command still works
    assert p.check("command_exec", {"command": "npm run build"}).level == "allow"


def test_persist_empty_command_does_not_create_global_bypass(tmp_path):
    p = _policy(tmp_path, tools={"command_exec": "require-approval"})
    import asyncio
    asyncio.run(p.persist_allow_always({"tool": "command_exec", "args": {"command": ""}}))
    assert p.check("command_exec", {"command": "rm -rf /"}).level == "deny"


def test_override_file_hot_reloads_on_mtime_change(tmp_path):
    p = _policy(tmp_path, tools={"web_fetch": "require-approval"})
    assert p.check("web_fetch", {"url": "http://x"}).level == "ask"
    import json
    (tmp_path / "ovr.json").write_text(json.dumps({"web_fetch": "allow"}), "utf-8")
    assert p.check("web_fetch", {"url": "http://x"}).level == "allow"
