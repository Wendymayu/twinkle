from pathlib import Path

import pytest
from pydantic import ValidationError

from twinkle.config.schema import (
    TwinkleConfig, PermissionsConfig, SkillsConfig, PermissionTier, SkillMode,
)


def test_defaults_match_packaged_yaml():
    c = TwinkleConfig()
    assert c.agentserver.host == "127.0.0.1"
    assert c.agentserver.port == 18000
    assert c.gateway.port == 19000
    assert c.llm.base_url == "https://api.openai.com/v1"
    assert c.llm.model == "gpt-4o-mini"
    assert c.agent.max_steps == 1000
    assert c.context_compression.token_threshold == 60000
    assert c.context_compression.keep_recent_pairs == 6
    assert c.context_compression.summary_prompt.startswith("你是对话上下文压缩器")
    assert c.skills.mode == "all"
    assert c.skills.enabled == []
    assert c.permissions.enabled is False
    assert c.permissions.enabled_channels == ["web"]
    assert c.permissions.global_default == "allow"
    assert c.permissions.tools["command_exec"] == "require-approval"
    assert c.permissions.rules == []


def test_bad_permission_tier_raises():
    with pytest.raises(ValidationError):
        PermissionsConfig(enabled=True, enabled_channels=["web"],
                           global_default="BOGUS", tools={"x": "allow"})


def test_bad_tool_tier_raises():
    with pytest.raises(ValidationError):
        PermissionsConfig(enabled=True, enabled_channels=["web"],
                           global_default="allow", tools={"x": "BOGUS"})


def test_bad_skill_mode_raises():
    with pytest.raises(ValidationError):
        SkillsConfig(mode="nope")


def test_derived_paths_under_default_workspace():
    c = TwinkleConfig()  # workspace.dir empty -> ~/.twinkle
    assert Path(c.workspace.dir).resolve() == (Path.home() / ".twinkle").resolve()
    assert c.sessions.dir.replace("\\", "/").endswith(".twinkle_data/sessions")
    assert c.todos.dir.replace("\\", "/").endswith(".twinkle_data/todos")
    assert c.logging.dir.replace("\\", "/").endswith("logs")
    assert c.skills.dir.replace("\\", "/").endswith("skills")
    assert c.permissions.overrides_file.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    assert c.permissions.audit_file.replace("\\", "/").endswith(
        "logs/audit/permission_audit.jsonl")


def test_derived_paths_under_custom_workspace():
    c = TwinkleConfig(workspace={"dir": "/tmp/twinkle-test"})
    assert c.sessions.dir.replace("\\", "/") == "/tmp/twinkle-test/.twinkle_data/sessions"
    assert c.todos.dir.replace("\\", "/") == "/tmp/twinkle-test/.twinkle_data/todos"
    assert c.logging.dir.replace("\\", "/") == "/tmp/twinkle-test/logs"
    assert c.skills.dir.replace("\\", "/") == "/tmp/twinkle-test/skills"
    assert c.permissions.audit_file.replace("\\", "/") == "/tmp/twinkle-test/logs/audit/permission_audit.jsonl"


def test_explicit_dirs_not_overwritten():
    c = TwinkleConfig(workspace={"dir": "/tmp/ws"},
                      sessions={"dir": "/tmp/sess"},
                      logging={"dir": "/tmp/logs"})
    assert c.sessions.dir == "/tmp/sess"
    assert c.logging.dir == "/tmp/logs"
    # permissions.audit_file derives from logging.dir
    assert c.permissions.audit_file.replace("\\", "/") == "/tmp/logs/audit/permission_audit.jsonl"


def test_workspace_tilde_expanded():
    c = TwinkleConfig(workspace={"dir": "~/twinkle-xyz"})
    assert "~" not in c.workspace.dir
    assert c.workspace.dir.replace("\\", "/").endswith("twinkle-xyz")


def test_explicit_path_tilde_expanded():
    c = TwinkleConfig(logging={"dir": "~/mylogs"})
    assert "~" not in c.logging.dir
    # audit_file derives from the resolved logging.dir — no literal ~
    assert "~" not in c.permissions.audit_file
    assert c.permissions.audit_file.replace("\\", "/").endswith(
        "mylogs/audit/permission_audit.jsonl")


def test_extra_keys_forbidden():
    # top-level section-name typo -> caught (not silently ignored)
    with pytest.raises(ValidationError):
        TwinkleConfig(**{"permission": {"enabled": True}})
    # within-section field typo -> caught
    with pytest.raises(ValidationError):
        PermissionsConfig(enabled=True, enabled_channels=["web"],
                             global_default="allow", tools={"x": "allow"}, bogus_field=1)
