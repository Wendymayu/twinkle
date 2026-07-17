"""ChannelManager — registers channels and runs the outbound dispatch loop.

Consumes outbound robot messages (chat.delta / chat.final events produced by
MessageHandler) and delivers each to the channel owning its channel_id.
Minimal mirror of jiuwenclaw/gateway/channel_manager.py:57-69 / :182-239.
"""
from __future__ import annotations

import asyncio
import logging

from twinkle.schema.message import Message

log = logging.getLogger("twinkle.gateway.channel_manager")


class ChannelManager:
    def __init__(self) -> None:
        self._message_handler = None  # set after MessageHandler is wired
        self._channels: dict[str, object] = {}
        self._outbound: asyncio.Queue[Message] = asyncio.Queue()
        self._dispatch_task: asyncio.Task | None = None

    def set_message_handler(self, message_handler) -> None:
        self._message_handler = message_handler

    def register_channel(self, channel) -> None:
        self._channels[channel.channel_id] = channel

        async def _on_message(msg: Message) -> bool:
            if self._message_handler is not None:
                await self._message_handler.handle_message(msg)
            return True

        channel.on_message(_on_message)

    async def publish_robot_message(self, msg: Message) -> None:
        await self._outbound.put(msg)

    async def _dispatch_loop(self) -> None:
        while True:
            msg = await self._outbound.get()
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
