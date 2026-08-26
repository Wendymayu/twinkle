"""权限配置现在从 resources/config.yaml 加载（不再用 TWINKLE_PERMISSIONS 环境变量）。

v1 移除了单个 JSON 环境变量；启用/覆盖改为编辑 YAML（或测试中让 load_config 指向自定义 YAML）。"""
import importlib

import pytest


def test_defaults_disabled(monkeypatch):
    monkeypatch.delenv("TWINKLE_PERMISSIONS", raising=False)  # 现在已无实际作用；保留以维持测试封闭性
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
    with pytest.raises(Exception):  # 即 pydantic 的 ValidationError
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
    # 提供 tools dict 时整体替换（对齐旧 _load_permissions 的浅合并语义）——
    # command_exec 的默认值不会自动并入。实际使用中用户编辑打包的 config.yaml
    # （其中包含完整 tools dict），因此保留 command_exec 的同时新增 echo。
    assert "command_exec" not in c.permissions.tools
