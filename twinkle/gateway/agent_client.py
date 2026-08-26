"""AgentServer 的 WebSocket client（Gateway 侧）。

连接到 AgentServer ws endpoint，先读 connection.ack 首帧，然后按 request_id 把
inbound 帧分路到 per-request asyncio.Queue。暴露 send_request_stream
（async generator）—— 只支持流式，无 unary 模式。

是 jiuwenclaw/gateway/agent_client.py:153 / :205 / :336 的精简镜像。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from websockets.asyncio.client import connect

from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.gateway.agent_client")


class AgentClient:
    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._ws = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._send_lock = asyncio.Lock()
        self._recv_task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def connect(self) -> None:
        self._ws = await connect(
            self._uri,
            ping_interval=30,
            ping_timeout=300,
            max_size=8 * 1024 * 1024,
        )
        # 首帧必须是 connection.ack
        raw = await self._ws.recv()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        if data.get("event") == EventType.CONNECTION_ACK.value:
            self._ready.set()
        else:
            log.warning("expected connection.ack, got: %s", raw)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("connected to AgentServer %s", self._uri)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                request_id = data.get("request_id")
                if not request_id:
                    continue
                request_queue = self._queues.get(request_id)
                if request_queue is not None:
                    await request_queue.put(data)
        except Exception as exc:
            log.warning("recv loop ended: %s", exc)
        finally:
            self._fail_pending("agent server disconnected")

    def _fail_pending(self, reason: str) -> None:
        """快速失败：向每个 pending request queue 推入一个 ConnectionError，使得被挂起的
        send_request_stream 在 recv loop 结束时（AgentServer 断开连接）能解除阻塞，
        而不是永远挂起。"""
        err = ConnectionError(reason)
        for request_queue in list(self._queues.values()):
            request_queue.put_nowait(err)

    async def _send(self, envelope: E2AEnvelope) -> None:
        async with self._send_lock:
            await self._ws.send(envelope.model_dump_json())

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        request_id = envelope.request_id
        request_queue: asyncio.Queue = asyncio.Queue()
        self._queues[request_id] = request_queue
        await self._send(envelope)
        try:
            while True:
                data = await request_queue.get()
                if isinstance(data, BaseException):
                    raise data  # recv loop 已结束 —— 快速失败而非挂起
                resp = E2AResponse.model_validate(data)
                yield resp
                if resp.is_final:
                    break
        finally:
            self._queues.pop(request_id, None)

    async def close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws is not None:
            await self._ws.close()
