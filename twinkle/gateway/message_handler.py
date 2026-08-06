"""MessageHandler — inbound routing + stream fan-out (Gateway side).

Inbound: a browser chat.send Message -> wrap as E2AEnvelope -> call the
AgentClient stream. Outbound: each E2A chunk becomes a chat.delta Message
(terminal chunk -> chat.final) put into the _robot_messages Queue for
ChannelManager dispatch. Stream-only; no unary mode.

Dependency direction (aligned with jiuwenclaw): MessageHandler only holds
AgentClient + its own outbound Queue. It does NOT hold ChannelManager.
ChannelManager consumes from this Queue via consume_robot_message().

Minimal mirror of jiuwenclaw/gateway/message_handler.py:2408-2484 (process_stream)
and jiuwenclaw's publish_robot_messages / consume_robot_messages Queue pattern.
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

    # --- outbound Queue (consumed by ChannelManager) ---
    # outbound = Agent responses flowing toward the browser.

    async def enqueue_outbound(self, msg: Message) -> None:
        """Put an outbound (Agent→browser) message into the Queue."""
        await self._robot_messages.put(msg)

    async def dequeue_outbound(self) -> Message:
        """Get the next outbound message from the Queue (blocking)."""
        return await self._robot_messages.get()
