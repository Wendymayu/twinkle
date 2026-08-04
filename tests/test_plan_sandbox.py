"""Tests for the sandbox module — exec(plan_code) isolation."""

from __future__ import annotations

import pytest

from twinkle.agentserver.workflow.sandbox import (
    _SAFE_BUILTINS,
    build_namespace,
    safe_import,
)


# ── _SAFE_BUILTINS tests ──────────────────────────────────────────


def test_safe_builtins_has_len() -> None:
    assert "len" in _SAFE_BUILTINS
    assert _SAFE_BUILTINS["len"] is len


def test_safe_builtins_no_open() -> None:
    assert "open" not in _SAFE_BUILTINS


def test_safe_builtins_no_exec() -> None:
    assert "exec" not in _SAFE_BUILTINS


def test_safe_builtins_no_eval() -> None:
    assert "eval" not in _SAFE_BUILTINS


def test_safe_builtins_no_getattr() -> None:
    assert "getattr" not in _SAFE_BUILTINS


# ── build_namespace tests ─────────────────────────────────────────


def test_build_namespace_has_plan_node() -> None:
    ns = build_namespace()
    # PlanNode may be None if node.py doesn't exist yet (Task 4)
    assert "PlanNode" in ns


def test_build_namespace_has_hook_interrupt() -> None:
    ns = build_namespace()
    from twinkle.agentserver.hooks.base import HookInterrupt

    assert ns["HookInterrupt"] is HookInterrupt


def test_build_namespace_replaces_builtins() -> None:
    ns = build_namespace()
    builtins = ns["__builtins__"]
    assert isinstance(builtins, dict)
    assert "open" not in builtins


# ── exec-in-sandbox integration tests ─────────────────────────────


def test_exec_in_sandbox_cannot_import_os() -> None:
    ns = build_namespace()
    with pytest.raises(ImportError, match="os"):
        exec("import os", ns)  # noqa: S102


def test_exec_in_sandbox_cannot_import_subprocess() -> None:
    ns = build_namespace()
    with pytest.raises(ImportError, match="subprocess"):
        exec("import subprocess", ns)  # noqa: S102


def test_exec_in_sandbox_can_use_safe_builtins() -> None:
    ns = build_namespace()
    exec("result = len([1, 2, 3])", ns)  # noqa: S102
    assert ns["result"] == 3


def test_exec_in_sandbox_cannot_open_file() -> None:
    ns = build_namespace()
    with pytest.raises(NameError, match="open"):
        exec("open('/etc/passwd')", ns)  # noqa: S102


def test_exec_in_sandbox_cannot_exec() -> None:
    ns = build_namespace()
    with pytest.raises(NameError, match="exec"):
        exec("exec('1+1')", ns)  # noqa: S102
