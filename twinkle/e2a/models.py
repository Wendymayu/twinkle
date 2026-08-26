"""E2A (Envelope-to-Agent) —— Gateway 与 AgentServer 之间的最小线协议。

取 jiuwenclaw E2A schema 的子集（e2a/models.py）。只保留流式 echo 循环所需
字段；完整 codec（legacy fallback、wire_codec、gateway_normalize）刻意
不再实现。

注意：Twinkle 采用纯流式模式——每个请求隐式为流式。E2AEnvelope 上的
`is_stream` 字段已移除；响应始终携带 `is_stream=True`。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

E2A_PROTOCOL_VERSION = "1.0"


class E2AEnvelope(BaseModel):
    """Gateway -> AgentServer 请求信封。

    Twinkle 为纯流式；没有 `is_stream` 字段——每个请求隐式为流式。
    """

    protocol_version: str = E2A_PROTOCOL_VERSION
    request_id: str
    channel: str = "web"
    session_id: str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = 0.0


class E2AResponse(BaseModel):
    """AgentServer -> Gateway 响应 / 分片。"""

    protocol_version: str = E2A_PROTOCOL_VERSION
    request_id: str
    sequence: int = 0
    is_final: bool = False
    status: str = "in_progress"  # in_progress | succeeded | failed
    response_kind: str = "e2a.chunk"  # e2a.chunk | e2a.complete | e2a.error | e2a.todo_update | e2a.result | e2a.ask
    body: dict[str, Any] = Field(default_factory=dict)
    is_stream: bool = True
