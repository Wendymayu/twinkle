"""Team ContextVar bridge — enables delegate_to_member to access the current Team."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from twinkle.agentserver.team.manager import Team

CURRENT_TEAM: ContextVar[Team | None] = ContextVar("team", default=None)

# Workspace override for team members — file tools check this before falling
# back to the global WORKSPACE_DIR. Set by Team._drive_member() so member
# writes land in the team shared directory, not the global workspace.
MEMBER_WORKSPACE: ContextVar[Path | None] = ContextVar("member_workspace", default=None)

# Current member's name — set in Team._drive_member()'s _run() so member tools
# (e.g. todo owner=) can read it. Task 3 only defines; set is wired in Task 7.
CURRENT_MEMBER_NAME: ContextVar[str | None] = ContextVar(
    "current_member_name", default=None)
