"""MessageHandler — inbound routing + stream fan-out (Gateway side).

Inbound: a browser chat.send Message -> wrap as E2AEnvelope -> call the
AgentClient stream. Outbound: each E2A chunk becomes a chat.delta Message
(terminal chunk -> chat.final) published to the ChannelManager for browser
broadcast. Stream-only; no unary mode.

Minimal mirror of jiuwenclaw/gateway/message_handler.py:2408-2484 (process_stream).
"""
from __future__ import annotations

import asyncio
import logging

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.schema.message import EventType, Message

log = logging.getLogger("twinkle.gateway.message_handler")


class MessageHandler:
    def __init__(self, agent_client: AgentClient, channel_manager: ChannelManager) -> None:
        self._agent_client = agent_client
        self._channel_manager = channel_manager

    async def handle_message(self, msg: Message) -> None:
        env = E2AEnvelope(
            request_id=msg.id,
            channel=msg.channel_id,
            session_id=msg.session_id,
            method=msg.method,
            params=msg.params,
        )
        asyncio.create_task(self._process_stream(env, msg))

    async def _process_stream(self, env: E2AEnvelope, msg: Message) -> None:
        try:
            async for resp in self._agent_client.send_request_stream(env):
                content = (resp.body.get("result") or {}).get("content", "")
                event_type = EventType.CHAT_FINAL if resp.is_final else EventType.CHAT_DELTA
                out = Message(
                    id=msg.id,
                    type="event",
                    channel_id=msg.channel_id,
                    session_id=msg.session_id,
                    event_type=event_type,
                    content=content,
                )
                await self._channel_manager.publish_robot_message(out)
        except Exception as exc:
            log.exception("process_stream failed for %s: %s", msg.id, exc)
            err = Message(
                id=msg.id,
                type="event",
                channel_id=msg.channel_id,
                session_id=msg.session_id,
                event_type=EventType.CHAT_FINAL,
                content=f"[error] {exc}",
            )
            await self._channel_manager.publish_robot_message(err)

