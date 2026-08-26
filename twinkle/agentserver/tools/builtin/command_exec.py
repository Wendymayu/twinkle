"""command_exec —— 在 workspace 中运行 shell 命令,返回输出。

jiuwenclaw/agentserver/tools/command_tools.py(353 行)的精简重写。
保留关键部分:跨平台 shell(Windows 上用 PowerShell,Unix 上用
bash/sh)、危险命令黑名单、workspace 受限 workdir、输出截断、超时、
非阻塞后台模式。去掉 shell_type 选择器 + token 嗅探自动检测(本地
学习工具一个 OS 一种 shell 足够)、runtime-venv/pip-env 机制,以及
`env` 额外参数。

非只读 —— 黑名单 + workspace 限制是当前唯一的安全护栏;
审批流程延后(roadmap `permissions/`)。
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
from twinkle.agentserver.tools.errors import ToolError
from twinkle.config import WORKSPACE_DIR

# --- 安全:deny 模式统一定义在唯一真源。 ---
from twinkle.agentserver.permissions.builtin_rules import matches as _command_deny_matches


def _clip_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _check_command_safety(command: str) -> str | None:
    """纵深防御:当权限系统关闭(或 hook 被绕过)时,这里仍用共享的
    builtin_rules 表(唯一真源)拒绝危险命令。"""
    return _command_deny_matches(command)


def _resolve_workdir(workdir: str) -> Path:
    """把 `workdir` 相对 WORKSPACE_DIR 解析;拒绝逃逸出 workspace 的路径。"""
    root = Path(WORKSPACE_DIR).resolve()
    candidate = Path(workdir) if workdir else root
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(root)  # raises ValueError if it escapes the workspace
    return candidate


def _resolve_execution_plan(command: str) -> tuple[Sequence[str], str]:
    """选择平台 shell。返回 (argv, resolved_shell_name)。"""
    if os.name == "nt":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command], "powershell"
    exe = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return [exe, "-c", command], "bash" if os.path.basename(exe) == "bash" else "sh"


def _run_command_sync(
    command: str, timeout_seconds: int, workdir: Path
) -> tuple[subprocess.CompletedProcess, str]:
    """薄 subprocess 钩子 —— 测试 monkeypatch 它以避免真实执行。"""
    plan, resolved_shell = _resolve_execution_plan(command)
    # Windows cmd/PS 输出常是系统代码页(CP936/GBK);按 UTF-8 解码
    # 非 ASCII 会乱码。回退到首选编码。
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
    """分离启动命令;返回 (pid, resolved_shell, error_msg)。"""
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
        pass  # 宽限期后仍在运行 -> 视为已启动
    return proc.pid, resolved_shell, None


@tool
async def command_exec(
    command: str,
    timeout_seconds: int = 300,
    workdir: str = ".",
    max_output_chars: int = 20000,
    background: bool = False,
) -> str:
    """在 workspace 中运行 shell 命令,以 JSON 返回其输出。

    跨平台:Windows 上用 PowerShell,Unix 上用 bash/sh。`workdir` 被限制在
    项目 workspace 根目录下。设 `background=True` 以非阻塞方式启动
    (返回一个 pid)。超过 `max_output_chars` 的输出会被截断
    (0 = 不限量)。
    """
    command = (command or "").strip()
    if not command:
        raise ToolError("command cannot be empty.", kind="validation")

    blocked_reason = _check_command_safety(command)
    if blocked_reason:
        raise ToolError(f"command rejected for safety ({blocked_reason}).", kind="denied")

    try:
        resolved_workdir = _resolve_workdir(workdir)
    except Exception:
        raise ToolError("workdir is outside the project workspace.", kind="validation")

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
            raise ToolError(f"command failed to start: {exc}", kind="failed")
        if err:
            raise ToolError(f"background command failed: {err}", kind="failed")
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
        raise ToolError(f"command timed out after {timeout_seconds}s.", kind="failed")
    except Exception as exc:
        raise ToolError(f"command execution failed: {exc}", kind="failed")

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
