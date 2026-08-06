"""Team subsystem — TeamManager + Team + MEMBER_TOOL_WHITELIST.

Phase A: 1 leader + dynamic-role members, hardcoded tool whitelist, shared workspace.
"""

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.manager import MEMBER_TOOL_WHITELIST, Team, TeamManager
from twinkle.agentserver.team.workspace import ensure_team_workspace, team_workspace_dir

__all__ = [
    "CURRENT_TEAM",
    "MEMBER_TOOL_WHITELIST",
    "Team",
    "TeamManager",
    "ensure_team_workspace",
    "team_workspace_dir",
]
