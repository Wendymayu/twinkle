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
    # Enable via the config constant permission_engine() reads fresh at call
    # time (TWINKLE_PERMISSIONS env was removed in v1; permissions are YAML-driven).
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    from twinkle.agentserver.permissions import permission_engine
    e = permission_engine()
    assert e._enabled is True
