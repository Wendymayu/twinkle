"""TeamContextHook — 在每次 invoke 前设置 CURRENT_TEAM ContextVar。

当 team.enabled 为 true 时由 create_agent 自动装配。镜像
SubagentContextHook 的模式：before_invoke → ContextVar.set，使
无参的 delegate_to_member tool 能在运行时读取 Team。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.team.context import CURRENT_TEAM

if TYPE_CHECKING:
    from twinkle.agentserver.team.manager import TeamManager


class TeamContextHook(AgentHook):
    """在每次 agent 调用前设置 CURRENT_TEAM ContextVar。"""

    priority = 45  # 在 SubagentContextHook(50) 之后运行（before_invoke 事件中靠后）

    def __init__(self, team_manager: "TeamManager") -> None:
        self._manager = team_manager

    async def before_invoke(self, ctx: HookContext) -> None:
        mode = getattr(ctx.inputs, "mode", "") or ""
        if mode == "team":
            team = self._manager.ensure_team(ctx.session_id)
            CURRENT_TEAM.set(team)
        else:
            CURRENT_TEAM.set(None)
