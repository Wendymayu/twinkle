"""Team workspace — member 文件交换的共享目录。"""
from __future__ import annotations

from pathlib import Path

from twinkle.config import WORKSPACE_DIR


def team_workspace_dir(session_id: str) -> Path:
    """返回某 session 的 team 共享 workspace 路径。"""
    return Path(WORKSPACE_DIR) / "team" / session_id / "shared"


def ensure_team_workspace(session_id: str) -> Path:
    """创建并返回 team 共享 workspace 目录（幂等）。"""
    d = team_workspace_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d
