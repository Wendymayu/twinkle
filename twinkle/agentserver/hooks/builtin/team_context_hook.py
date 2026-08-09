"""TeamContextHook — sets CURRENT_TEAM ContextVar before each invoke.

Auto-wired by create_agent when team.enabled is true. Mirrors the pattern
of SubagentContextHook: before_invoke → ContextVar.set, so the
parameter-less delegate_to_member tool can read the Team at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.team.context import CURRENT_TEAM

if TYPE_CHECKING:
    from twinkle.agentserver.team.manager import TeamManager


class TeamContextHook(AgentHook):
    """Set CURRENT_TEAM ContextVar before each agent invocation."""

    priority = 45  # runs before SubagentContextHook (50) and most others

    def __init__(self, team_manager: "TeamManager") -> None:
        self._manager = team_manager

    async def before_invoke(self, ctx: HookContext) -> None:
        mode = getattr(ctx.inputs, "mode", "") or ""
        if mode == "team":
            team = self._manager.ensure_team(ctx.session_id)
            CURRENT_TEAM.set(team)
        else:
            CURRENT_TEAM.set(None)
