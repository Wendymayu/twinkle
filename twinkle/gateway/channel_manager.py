"""ChannelManager —— 注册 channel 并跑 outbound 分发循环。

从 MessageHandler 的 _robot_messages Queue 消费 outbound robot message，并把每条
投递给拥有其 channel_id 的 channel。inbound：每个已注册 Channel 的 on_message
回调路由到 MessageHandler。

依赖方向（对齐 jiuwenclaw）：ChannelManager 持有 MessageHandler（单向）。
MessageHandler 不持有 ChannelManager —— 它发布到自己的 Queue，ChannelManager
从该 Queue 消费。

是 jiuwenclaw/gateway/channel_manager.py:57-69 / :182-239 的精简镜像。
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
            channel = self._channels.get(msg.channel_id)
            if channel is None:
                continue
            try:
                await channel.send(msg)
            except Exception as exc:
                log.exception("dispatch error on %s: %s", msg.channel_id, exc)

    async def start(self) -> None:
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
