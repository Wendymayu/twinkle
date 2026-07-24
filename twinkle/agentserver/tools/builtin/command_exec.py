"""command_exec — run a shell command in the workspace, return output.

Slim rewrite of jiuwenclaw/agentserver/tools/command_tools.py (353 lines).
Keeps the load-bearing bits: cross-platform shell (PowerShell on Windows,
bash/sh on Unix), dangerous-command blocklist, workspace-confined workdir,
output clipping, timeout, and non-blocking background mode. Drops the
shell_type selector + token-sniffing auto-detection (one shell per OS is
enough for a local learning tool), the runtime-venv/pip-env machinery, and
the `env` extra param.

NOT read-only — blocklist + workspace confinement are the only safety rails
today; an approval flow is deferred (roadmap `permissions/`).
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from twinkle.agentserver.tools.decorator import tool
from twinkle.config import WORKSPACE_DIR

# --- Safety: deny patterns live in the single source of truth. ---
from twinkle.agentserver.permissions.builtin_rules import matches as _command_deny_matches


def _clip_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _check_command_safety(command: str) -> str | None:
    """Defense-in-depth: when the permission system is disabled (or the hook
    is bypassed), this still rejects dangerous commands using the shared
    builtin_rules table (single source of truth)."""
    return _command_deny_matches(command)


def _resolve_workdir(workdir: str) -> Path:
    """Resolve `workdir` against WORKSPACE_DIR; reject paths escaping it."""
    root = Path(WORKSPACE_DIR).resolve()
    candidate = Path(workdir) if workdir else root
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(root)  # raises ValueError if it escapes the workspace
    return candidate


def _resolve_execution_plan(command: str) -> tuple[Sequence[str], str]:
    """Pick the platform shell. Returns (argv, resolved_shell_name)."""
    if os.name == "nt":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command], "powershell"
    exe = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return [exe, "-c", command], "bash" if os.path.basename(exe) == "bash" else "sh"


def _run_command_sync(
    command: str, timeout_seconds: int, workdir: Path
) -> tuple[subprocess.CompletedProcess, str]:
    """Thin subprocess hook — tests monkeypatch this to avoid real execution."""
    plan, resolved_shell = _resolve_execution_plan(command)
    # Windows cmd/PS output is often the system codepage (CP936/GBK); decoding
    # as UTF-8 would mojibake non-ASCII. Fall back to the preferred encoding.
    encoding = locale.getpreferredencoding(False) or "utf-8"
    result = subprocess.run(
        plan,
        cwd=str(workdir),
        text=True,
        encoding=encoding,
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
    )
    return result, resolved_shell


def _run_command_background(
    command: str, workdir: Path, grace_seconds: float = 5.0
) -> tuple[int, str, str | None]:
    """Start command detached; return (pid, resolved_shell, error_msg)."""
    plan, resolved_shell = _resolve_execution_plan(command)
    proc = subprocess.Popen(
        plan,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        exit_code = proc.wait(timeout=grace_seconds)
        if exit_code != 0:
            return proc.pid, resolved_shell, f"Process exited with code {exit_code}"
    except subprocess.TimeoutExpired:
        pass  # still running after grace period -> considered started
    return proc.pid, resolved_shell, None


@tool
async def command_exec(
    command: str,
    timeout_seconds: int = 300,
    workdir: str = ".",
    max_output_chars: int = 20000,
    background: bool = False,
) -> str:
    """Run a shell command in the workspace and return its output as JSON.

    Cross-platform: PowerShell on Windows, bash/sh on Unix. The `workdir` is
    confined under the project workspace root. Set `background=True` to start
    non-blocking (returns a pid). Output beyond `max_output_chars` is clipped
    (0 = no limit).
    """
    command = (command or "").strip()
    if not command:
        return "[ERROR]: command cannot be empty."

    blocked_reason = _check_command_safety(command)
    if blocked_reason:
        return f"[ERROR]: command rejected for safety ({blocked_reason})."

    try:
        resolved_workdir = _resolve_workdir(workdir)
    except Exception:
        return "[ERROR]: workdir is outside the project workspace."

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 300
    timeout_seconds = max(1, min(timeout_seconds, 3600))
    try:
        max_output_chars = int(max_output_chars)
    except (TypeError, ValueError):
        max_output_chars = 20000
    if max_output_chars < 0:
        max_output_chars = 0

    if background:
        try:
            pid, resolved_shell, err = await asyncio.to_thread(
                _run_command_background, command, resolved_workdir
            )
        except Exception as exc:
            return f"[ERROR]: command failed to start: {exc}"
        if err:
            return f"[ERROR]: background command failed: {err}"
        return json.dumps(
            {
                "command": command,
                "cwd": str(resolved_workdir),
                "resolved_shell": resolved_shell,
                "pid": pid,
                "status": "started",
            },
            ensure_ascii=False,
        )

    try:
        result, resolved_shell = await asyncio.to_thread(
            _run_command_sync, command, timeout_seconds, resolved_workdir
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR]: command timed out after {timeout_seconds}s."
    except Exception as exc:
        return f"[ERROR]: command execution failed: {exc}"

    return json.dumps(
        {
            "command": command,
            "cwd": str(resolved_workdir),
            "resolved_shell": resolved_shell,
            "exit_code": result.returncode,
            "stdout": _clip_text(result.stdout or "", max_output_chars),
            "stderr": _clip_text(result.stderr or "", max_output_chars),
        },
        ensure_ascii=False,
    )
