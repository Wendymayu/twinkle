"""Browser-facing WebSocket channel (Gateway side).

A `websockets` server (default :19000). Inbound frames: {type:req,id,method,params}.
Outbound: an immediate {type:res,id,ok,payload} ACK, then streamed
{type:event,event:chat.delta|chat.final,payload,request_id} broadcasts.

In Phase 0 dev mode the Vite dev server (:5173) proxies /ws here, so the
browser stays same-origin. Minimal mirror of jiuwenclaw/channel/web_channel.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from websockets.asyncio.server import serve

from twinkle.schema.message import EventType, Message

log = logging.getLogger("twinkle.gateway.web_channel")

InboundCallback = Callable[[Message], "Awaitable[bool]"]


class WebChannel:
    channel_id = "web"

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._clients: set = set()
        self._on_message: InboundCallback | None = None

    def on_message(self, cb: InboundCallback) -> None:
        self._on_message = cb

    async def handler(self, ws) -> None:
        self._clients.add(ws)
        try:
            await self._send_event(ws, EventType.CONNECTION_ACK, {"status": "ready"})
            async for raw in ws:
                await self._handle_raw(ws, raw)
        except Exception as exc:
            log.debug("web client disconnected: %s", exc)
        finally:
            self._clients.discard(ws)

    async def _handle_raw(self, ws, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except Exception:
            return
        if frame.get("type") != "req":
            return
        rid = frame.get("id")
        method = frame.get("method", "chat.send")
        params = frame.get("params") or {}
        session_id = params.get("session_id")
        msg = Message(
            id=rid,
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            method=method,
            params=params,
            is_stream=True,
        )
        # immediate acceptance, like jiuwenclaw app_web_handlers _chat_send ACK
        await self._send_response(ws, rid, {"accepted": True, "session_id": session_id})
        if self._on_message is not None:
            await self._on_message(msg)

    async def send(self, msg: Message) -> None:
        """Broadcast an outbound event to all connected browsers, tagged with request_id."""
        event = msg.event_type.value if msg.event_type else EventType.CHAT_FINAL.value
        payload = dict(msg.payload)
        if msg.content:
            payload.setdefault("content", msg.content)
        frame = {
            "type": "event",
            "event": event,
            "payload": payload,
            "request_id": msg.id,
        }
        if self._clients:
            blob = json.dumps(frame, ensure_ascii=False)
            await asyncio.gather(
                *(c.send(blob) for c in self._clients), return_exceptions=True
            )

    async def _send_response(self, ws, rid: str, payload: dict, ok: bool = True) -> None:
        frame = {"type": "res", "id": rid, "ok": ok, "payload": payload}
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _send_event(self, ws, event: EventType, payload: dict) -> None:
        frame = {"type": "event", "event": event.value, "payload": payload}
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def start(self) -> None:
        log.info("WebChannel listening on %s:%s", self._host, self._port)
        async with serve(self.handler, self._host, self._port):
            await asyncio.Future()  # run forever
