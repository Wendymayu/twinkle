"""E2A (Envelope-to-Agent) — minimal wire protocol between Gateway and AgentServer.

Subset of jiuwenclaw's E2A schema (e2a/models.py). We keep only the fields
needed for a streaming/unary echo loop; the full codec (legacy fallback,
wire_codec, gateway_normalize) is intentionally not reimplemented.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

E2A_PROTOCOL_VERSION = "1.0"


class E2AEnvelope(BaseModel):
    """Gateway -> AgentServer request envelope."""

    protocol_version: str = E2A_PROTOCOL_VERSION
    request_id: str
    channel: str = "web"
    session_id: str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    is_stream: bool = False
    timestamp: float = 0.0


class E2AResponse(BaseModel):
    """AgentServer -> Gateway response / chunk."""

    protocol_version: str = E2A_PROTOCOL_VERSION
    request_id: str
    sequence: int = 0
    is_final: bool = False
    status: str = "in_progress"  # in_progress | succeeded | failed
    response_kind: str = "e2a.chunk"  # e2a.chunk | e2a.complete | e2a.error
    body: dict[str, Any] = Field(default_factory=dict)
    is_stream: bool = True
