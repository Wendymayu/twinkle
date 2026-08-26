"""MessageHandler —— inbound 路由 + stream fan-out（Gateway 侧）。

inbound：浏览器 chat.send Message -> 包装成 E2AEnvelope -> 调用 AgentClient
stream。outbound：每个 E2A chunk 变成一个 chat.delta Message（末个 chunk ->
chat.final）放入 _robot_messages Queue 供 ChannelManager 分发。只支持流式；
无 unary 模式。

依赖方向（对齐 jiuwenclaw）：MessageHandler 只持有 AgentClient + 自己的
outbound Queue。它不持有 ChannelManager。ChannelManager 通过
dequeue_outbound() 从该 Queue 消费。

是 jiuwenclaw/gateway/message_handler.py:2408-2484（process_stream）及
jiuwenclaw 的 publish_robot_messages / consume_robot_messages Queue 模式的精简镜像。
"""
from __future__ import annotations

import asyncio
import logging

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.agent_client import AgentClient
from twinkle.schema.message import EventType, Message

log = logging.getLogger("twinkle.gateway.message_handler")


class MessageHandler:
    def __init__(self, agent_client: AgentClient) -> None:
        self._agent_client = agent_client
        self._robot_messages: asyncio.Queue[Message] = asyncio.Queue()

    async def handle_message(self, msg: Message) -> None:
        envelope = E2AEnvelope(
            request_id=msg.id,
            channel=msg.channel_id,
            session_id=msg.session_id,
            method=msg.method,
            params=msg.params,
        )
        asyncio.create_task(self._process_stream(envelope, msg))

    async def _process_stream(self, envelope: E2AEnvelope, msg: Message) -> None:
        try:
            async for response in self._agent_client.send_request_stream(envelope):
                if response.response_kind == "e2a.todo_update":
                    outbound_msg = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=EventType.TODO_UPDATE,
                        payload=dict(response.body),
                        content="",
                    )
                elif response.response_kind == "e2a.ask":
                    outbound_msg = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=EventType.APPROVAL_ASK,
                        payload=dict(response.body),
                        content="",
                    )
                elif response.response_kind == "e2a.result":
                    outbound_msg = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=EventType.RESULT,
                        payload=dict(response.body),
                        content="",
                    )
                elif response.response_kind == "e2a.error":
                    outbound_msg = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=EventType.CHAT_FINAL,
                        content=f"[error] {response.body.get('error', '')}",
                        payload=dict(response.body),
                    )
                else:
                    content = (response.body.get("result") or {}).get("content", "")
                    event_type = EventType.CHAT_FINAL if response.is_final else EventType.CHAT_DELTA
                    outbound_msg = Message(
                        id=msg.id,
                        type="event",
                        channel_id=msg.channel_id,
                        session_id=msg.session_id,
                        event_type=event_type,
                        content=content,
                    )
                await self.enqueue_outbound(outbound_msg)
        except Exception as exc:
            log.exception("process_stream failed for %s: %s", msg.id, exc)
            error_message = Message(
                id=msg.id,
                type="event",
                channel_id=msg.channel_id,
                session_id=msg.session_id,
                event_type=EventType.CHAT_FINAL,
                content=f"[error] {exc}",
            )
            await self.enqueue_outbound(error_message)

    # --- outbound Queue（由 ChannelManager 消费）---
    # outbound = 流向浏览器的 Agent 响应。

    async def enqueue_outbound(self, msg: Message) -> None:
        """把一个 outbound（Agent→browser）message 放入 Queue。"""
        await self._robot_messages.put(msg)

    async def dequeue_outbound(self) -> Message:
        """从 Queue 取下一个 outbound message（阻塞）。"""
        return await self._robot_messages.get()
