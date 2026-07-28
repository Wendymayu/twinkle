"""Subagent data models + excluded-tool set + soft-timeout marker.

EXCLUDED_TOOLS is the recursion + capability guard: the child's ToolManager
copies the parent's tools minus this set. spawn_subagent => no recursion
(single layer); write_memory/edit_memory => child memory is read-only.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class SoftTimeoutError(Exception):
    """No child streaming activity for soft_timeout seconds."""


EXCLUDED_TOOLS: set[str] = {
    "spawn_subagent",          # recursion guard: child cannot delegate
    "write_memory",            # child memory is read-only
    "edit_memory",
}


class SubagentTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(default_factory=lambda: f"subagent_{uuid.uuid4().hex[:8]}")
    role_id: str = "MainAgent"      # v1: display label only, no role registry
    objective: str
    prompt: str = ""
    model_name: str = ""


class SubagentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    task_id: str
    role_id: str
    result: str | None = None
    error: str | None = None
