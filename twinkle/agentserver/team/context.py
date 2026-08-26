"""Team ContextVar 桥接 — 让 delegate_to_member 能访问当前 Team。"""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from twinkle.agentserver.team.manager import Team

CURRENT_TEAM: ContextVar[Team | None] = ContextVar("team", default=None)

# team member 的 workspace 覆盖 — file 工具会先检查它，再回退到
# 全局 WORKSPACE_DIR。由 Team._drive_member() 设置，使 member
# 写入落到 team 共享目录而非全局 workspace。
MEMBER_WORKSPACE: ContextVar[Path | None] = ContextVar("member_workspace", default=None)

# 当前 member 的名字 — 在 Team._drive_member() 的 _run() 中设置，使 member 工具
# （如 todo owner=）能读到它。Task 3 仅定义；赋值在 Task 7 接入。
CURRENT_MEMBER_NAME: ContextVar[str | None] = ContextVar(
    "current_member_name", default=None)
