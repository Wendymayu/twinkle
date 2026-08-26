"""permissions 包入口 re-exports + permission_engine() builder 的测试 (Task 9)。"""
from __future__ import annotations


def test_reexports():
    from twinkle.agentserver.permissions import (
        PermissionEngine, PermissionPolicy, ToolPermissionLog, ApprovalRegistry,
        APPROVAL_REGISTRY, PermissionDecision, PermissionLevel, matches)
    assert PermissionLevel.ASK == "ask"


def test_builder_uses_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import importlib, twinkle.config as cfg
    importlib.reload(cfg)
    # 通过 config 常量启用——permission_engine() 在调用时实时读取（v1 已移除
    # TWINKLE_PERMISSIONS 环境变量；权限由 YAML 驱动）。
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    from twinkle.agentserver.permissions import permission_engine
    e = permission_engine()
    assert e._enabled is True
