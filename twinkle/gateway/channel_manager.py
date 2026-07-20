"""ChannelManager — registers channels and runs the outbound dispatch loop.

Consumes outbound robot messages from MessageHandler's _robot_messages Queue
and delivers each to the channel owning its channel_id.
Inbound: each registered Channel's on_message callback routes to MessageHandler.

Dependency direction (aligned with jiuwenclaw): ChannelManager holds
MessageHandler (unidirectional). MessageHandler does NOT hold ChannelManager —
it publishes to its own Queue, and ChannelManager consumes from it.

Minimal mirror of jiuwenclaw/gateway/channel_manager.py:57-69 / :182-239.
"""
from __future__ import annotations

import asyncio
import logging

from twinkle.gateway.message_handler import MessageHandler
from twinkle.schema.message import Message

log = logging.getLogger("twinkle.gateway.channel_manager")


class ChannelManager:
    def __init__(self, message_handler: MessageHandler) -> None:
        self._message_handler = message_handler
        self._channels: dict[str, object] = {}
        self._dispatch_task: asyncio.Task | None = None

    def register_channel(self, channel) -> None:
        self._channels[channel.channel_id] = channel

        async def _on_message(msg: Message) -> bool:
            await self._message_handler.handle_message(msg)
            return True

        channel.on_message(_on_message)

    async def _dispatch_loop(self) -> None:
        while True:
            msg = await self._message_handler.dequeue_outbound()
            ch = self._channels.get(msg.channel_id)
            if ch is None:
                continue
            try:
                await ch.send(msg)
            except Exception as exc:
                log.exception("dispatch error on %s: %s", msg.channel_id, exc)

    async def start(self) -> None:
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
