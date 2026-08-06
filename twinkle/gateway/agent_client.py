"""AgentServer WebSocket client (Gateway side).

Connects to the AgentServer ws endpoint, reads the connection.ack first frame,
then demuxes inbound frames by request_id into per-request asyncio.Queues.
Exposes send_request_stream (async generator) — stream-only, no unary mode.

Minimal mirror of jiuwenclaw/gateway/agent_client.py:153 / :205 / :336.
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
        # first frame must be connection.ack
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
        """Fail-fast: push a ConnectionError into every pending request queue so
        a suspended send_request_stream unblocks instead of hanging forever when
        the recv loop ends (AgentServer disconnected)."""
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
                    raise data  # recv loop ended — fail fast instead of hanging
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
