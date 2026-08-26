"""内部 Message + EventType —— jiuwenclaw/schema/message.py 的子集。

只保留 Phase 0 所需的 chat.* 与 connection.ack 事件。
纯流式；无 `is_stream` 字段——每个请求隐式为流式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    CONNECTION_ACK = "connection.ack"
    CHAT_DELTA = "chat.delta"
    CHAT_FINAL = "chat.final"
    TODO_UPDATE = "todo.update"
    RESULT = "result"
    APPROVAL_ASK = "approval.ask"


@dataclass
class Message:
    """流经 gateway 的在途消息。

    id         —— 浏览器请求 id（用于关联流式分片）。
    type       —— "req"（来自浏览器的入站）| "event"（发往浏览器的出站）。
    channel_id —— 该消息所属的 channel（Phase 0：恒为 "web"）。
    event_type —— 用于出站事件消息（chat.delta / chat.final）。
    content    —— 文本载荷（delta 文本或最终文本）。
    """

    id: str
    type: str = "req"
    channel_id: str = "web"
    session_id: str | None = None
    method: str = "chat.send"
    params: dict[str, Any] = field(default_factory=dict)
    event_type: EventType | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    content: str = ""
