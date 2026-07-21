import asyncio
import json
import subprocess

from twinkle.agentserver.tools import command_exec


def _fake_completed(stdout: str = "", stderr: str = "", code: int = 0):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=code, stdout=stdout, stderr=stderr
    )


# --- safety + workdir guards ---

def test_rejects_empty_command() -> None:
    assert asyncio.run(command_exec.command_exec("")) == "[ERROR]: command cannot be empty."


def test_blocks_dangerous_pattern() -> None:
    out = asyncio.run(command_exec.command_exec("rm -rf /"))
    assert "rejected for safety" in out
    assert "rm -rf" in out


def test_rejects_workdir_escape() -> None:
    out = asyncio.run(command_exec.command_exec("echo hi", workdir="../../"))
    assert "outside the project workspace" in out


# --- foreground / background execution (mocked subprocess seam) ---

def test_runs_command_and_returns_json(monkeypatch) -> None:
    captured = {}

    def fake_run_sync(command, timeout_seconds, workdir):
        captured["command"] = command
        return _fake_completed(stdout="hello\n"), "sh"

    monkeypatch.setattr(command_exec, "_run_command_sync", fake_run_sync)

    payload = json.loads(asyncio.run(command_exec.command_exec("echo hello")))
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "hello\n"
    assert payload["stderr"] == ""
    assert payload["resolved_shell"] == "sh"
    assert captured["command"] == "echo hello"


def test_clips_large_output(monkeypatch) -> None:
    big = "x" * 100
    monkeypatch.setattr(
        command_exec,
        "_run_command_sync",
        lambda c, t, w: (_fake_completed(stdout=big), "sh"),
    )
    payload = json.loads(asyncio.run(command_exec.command_exec("echo big", max_output_chars=10)))
    assert payload["stdout"] == "xxxxxxxxxx\n...[truncated]"


def test_timeout_returns_error(monkeypatch) -> None:
    def fake_run_sync(command, timeout_seconds, workdir):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds)

    monkeypatch.setattr(command_exec, "_run_command_sync", fake_run_sync)
    out = asyncio.run(command_exec.command_exec("sleep 999", timeout_seconds=1))
    assert "timed out after 1s" in out


def test_background_returns_pid(monkeypatch) -> None:
    monkeypatch.setattr(
        command_exec, "_run_command_background", lambda c, w: (4242, "powershell", None)
    )
    payload = json.loads(asyncio.run(command_exec.command_exec("python -m http.server", background=True)))
    assert payload["pid"] == 4242
    assert payload["status"] == "started"


def test_background_failure_returns_error(monkeypatch) -> None:
    monkeypatch.setattr(
        command_exec,
        "_run_command_background",
        lambda c, w: (1, "powershell", "Process exited with code 1"),
    )
    out = asyncio.run(command_exec.command_exec("badcmd", background=True))
    assert "background command failed" in out


# --- cross-platform shell selection (no real execution needed) ---

def test_windows_uses_powershell(monkeypatch) -> None:
    monkeypatch.setattr(command_exec.os, "name", "nt")
    monkeypatch.setattr(command_exec.shutil, "which", lambda name: "/pwr/pwsh.exe")

    plan, shell = command_exec._resolve_execution_plan("Get-Process")
    assert shell == "powershell"
    assert plan == ["/pwr/pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Process"]


def test_unix_prefers_bash(monkeypatch) -> None:
    monkeypatch.setattr(command_exec.os, "name", "posix")
    monkeypatch.setattr(
        command_exec.shutil, "which", lambda name: "/usr/bin/bash" if name == "bash" else None
    )
    plan, shell = command_exec._resolve_execution_plan("ls -la")
    assert shell == "bash"
    assert plan == ["/usr/bin/bash", "-c", "ls -la"]


def test_unix_falls_back_to_sh(monkeypatch) -> None:
    monkeypatch.setattr(command_exec.os, "name", "posix")
    # No bash, no sh on PATH -> falls back to /bin/sh.
    monkeypatch.setattr(command_exec.shutil, "which", lambda name: None)
    plan, shell = command_exec._resolve_execution_plan("uname -a")
    assert shell == "sh"
    assert plan == ["/bin/sh", "-c", "uname -a"]
