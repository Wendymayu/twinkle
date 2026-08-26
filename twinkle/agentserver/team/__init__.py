"""Team subsystem — TeamManager + Team + MEMBER_TOOL_WHITELIST。

Phase 18：1 个 leader + 动态角色 member，硬编码 tool 白名单，共享 workspace。
"""

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.manager import MEMBER_TOOL_WHITELIST, Team, TeamManager
from twinkle.agentserver.team.message_box import MessageBox
from twinkle.agentserver.team.task_store import TeamTaskStore
from twinkle.agentserver.team.workspace import ensure_team_workspace, team_workspace_dir

__all__ = [
    "CURRENT_TEAM",
    "MEMBER_TOOL_WHITELIST",
    "MessageBox",
    "Team",
    "TeamManager",
    "TeamTaskStore",
    "ensure_team_workspace",
    "team_workspace_dir",
]
