"""Team workspace — shared directory for member file exchange."""
from __future__ import annotations

from pathlib import Path

from twinkle.config import WORKSPACE_DIR


def team_workspace_dir(session_id: str) -> Path:
    """Return the team shared workspace path for a session."""
    return Path(WORKSPACE_DIR) / "team" / session_id / "shared"


def ensure_team_workspace(session_id: str) -> Path:
    """Create and return the team shared workspace directory (idempotent)."""
    d = team_workspace_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d
