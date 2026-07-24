import importlib
import os


def test_defaults_disabled(monkeypatch):
    # Force empty (not just delenv) so the test is hermetic w.r.t. a real .env
    # that might set TWINKLE_PERMISSIONS — empty falls through to defaults.
    monkeypatch.setenv("TWINKLE_PERMISSIONS", "")
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


def test_bare_true_enables(monkeypatch):
    # `TWINKLE_PERMISSIONS=true` (a bare bool, like OTEL_ENABLED) must enable,
    # not crash the config import (regression: json.loads("true") -> bool -> update crash).
    monkeypatch.setenv("TWINKLE_PERMISSIONS", "true")
    import twinkle.config as cfg
    importlib.reload(cfg)
    assert cfg.PERMISSIONS_ENABLED is True
    # default tools preserved
    assert cfg.PERMISSIONS_TOOLS.get("command_exec") == "require-approval"


def test_bare_yes_and_1_enable(monkeypatch):
    for val in ("yes", "1"):
        monkeypatch.setenv("TWINKLE_PERMISSIONS", val)
        import twinkle.config as cfg
        importlib.reload(cfg)
        assert cfg.PERMISSIONS_ENABLED is True, f"{val} should enable"


def test_bare_false_disables(monkeypatch):
    for val in ("false", "no", "0"):
        monkeypatch.setenv("TWINKLE_PERMISSIONS", val)
        import twinkle.config as cfg
        importlib.reload(cfg)
        assert cfg.PERMISSIONS_ENABLED is False, f"{val} should disable"


def test_non_dict_json_does_not_crash(monkeypatch):
    # a JSON value that isn't an object (e.g. a bare number) must fall back to
    # defaults, NOT raise TypeError on dict.update.
    monkeypatch.setenv("TWINKLE_PERMISSIONS", "42")
    import twinkle.config as cfg
    importlib.reload(cfg)  # would raise TypeError before the fix
    assert cfg.PERMISSIONS_ENABLED is False  # safe default
