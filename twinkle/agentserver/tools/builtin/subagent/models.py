"""Subagent data models + excluded-tool set + soft-timeout marker.

EXCLUDED_TOOLS is the recursion + capability guard: the child's ToolManager
copies the parent's tools minus this set. spawn_subagent => no recursion
(single layer); write_memory/edit_memory => child memory is read-only.
"""
from __future__ import annotations

from pydantic import BaseModel


class SoftTimeoutError(Exception):
    """No child streaming activity for soft_timeout seconds."""


EXCLUDED_TOOLS: set[str] = {
    "spawn_subagent",          # recursion guard: child cannot delegate
    "write_memory",            # child memory is read-only
    "edit_memory",
}


class SubagentTaskSpec(BaseModel):
    objective: str
    prompt: str = ""


class SubagentResult(BaseModel):
    success: bool
    result: str | None = None
    error: str | None = None
