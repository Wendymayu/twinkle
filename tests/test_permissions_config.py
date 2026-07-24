import importlib
import os


def test_defaults_disabled(monkeypatch):
    monkeypatch.delenv("TWINKLE_PERMISSIONS", raising=False)
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is False
    assert cfg.PERMISSIONS_ENABLED_CHANNELS == {"web"}
    assert cfg.PERMISSIONS_TOOLS.get("command_exec") == "require-approval"
    assert cfg.PERMISSIONS_GLOBAL_DEFAULT == "allow"


def test_enabled_via_json(monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", '{"enabled": true, "tools": {"echo": "deny"}}')
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is True
    assert cfg.PERMISSIONS_TOOLS["echo"] == "deny"


def test_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", "{not json")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is False  # fell back to defaults


def test_override_paths_under_workspace(monkeypatch):
    monkeypatch.delenv("TWINKLE_PERMISSIONS", raising=False)
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", "/tmp/twinkle-test")
    import twinkle.config as cfg
    importlib.reload(cfg)
    # normalize separators so the assertion is cross-platform (Windows uses \)
    assert cfg.PERMISSION_OVERRIDES_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_overrides.json"
    )
    assert cfg.PERMISSION_AUDIT_FILE.replace("\\", "/").endswith(
        ".twinkle_data/permission_audit.jsonl"
    )
