"""Internal Message + EventType — subset of jiuwenclaw/schema/message.py.

Only the chat.* and connection.ack events needed for Phase 0 are retained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    CONNECTION_ACK = "connection.ack"
    CHAT_DELTA = "chat.delta"
    CHAT_FINAL = "chat.final"


@dataclass
class Message:
    """An in-flight message flowing through the gateway.

    id         — the browser request id (used to correlate streaming chunks).
    type       — "req" (inbound from browser) | "event" (outbound to browser).
    channel_id — which channel this message belongs to (Phase 0: always "web").
    event_type — for outbound event messages (chat.delta / chat.final).
    content    — text payload (delta text or final text).
    """

    id: str
    type: str = "req"
    channel_id: str = "web"
    session_id: str | None = None
    method: str = "chat.send"
    params: dict[str, Any] = field(default_factory=dict)
    is_stream: bool = True
    event_type: EventType | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    content: str = ""
