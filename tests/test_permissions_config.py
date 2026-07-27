"""Permission config now loads from resources/config.yaml (no TWINKLE_PERMISSIONS env).

v1 dropped the single JSON env var; enabling/overrides are done by editing the
YAML (or pointing load_config at a custom YAML in tests)."""
import importlib

import pytest


def test_defaults_disabled(monkeypatch):
    monkeypatch.delenv("TWINKLE_PERMISSIONS", raising=False)  # no-op now; kept hermetic
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is False
    assert cfg.PERMISSIONS_ENABLED_CHANNELS == {"web"}
    assert cfg.PERMISSIONS_TOOLS.get("command_exec") == "require-approval"
    assert cfg.PERMISSIONS_GLOBAL_DEFAULT == "allow"
    assert cfg.PERMISSIONS_RULES == []


def test_override_paths_under_workspace(monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", "/tmp/twinkle-test")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSION_OVERRIDES_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json")
    assert cfg.PERMISSION_AUDIT_FILE.replace("\\", "/").endswith(
        "logs/audit/permission_audit.jsonl")
    monkeypatch.delenv("TWINKLE_WORKSPACE_DIR", raising=False)
    importlib.reload(cfg)


def test_bad_tier_in_config_raises(tmp_path):
    from twinkle.config.loader import load_config
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  global_default: BOGUS\n", encoding="utf-8")
    with pytest.raises(Exception):  # pydantic ValidationError
        load_config(custom)


def test_enable_and_tool_override_via_yaml(tmp_path):
    from twinkle.config.loader import load_config
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  enabled: true\n  tools:\n    echo: deny\n",
        encoding="utf-8")
    c = load_config(custom)
    assert c.permissions.enabled is True
    assert c.permissions.tools["echo"] == "deny"
    # tools dict is replaced wholesale when provided (matches the old
    # _load_permissions shallow-merge semantics) — command_exec's default is
    # NOT auto-merged in. In practice a user edits the packaged config.yaml
    # (which carries the full tools dict) so they keep command_exec + add echo.
    assert "command_exec" not in c.permissions.tools
