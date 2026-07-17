"""AgentServer — the heavy execution core process.

Phase 0: a `websockets` server (default :18000) that speaks the E2A subset
and echoes inbound requests back as streaming chunks + a final frame. No real
agent runtime yet — the echo handler is inline. This mirrors jiuwenclaw's
agentserver/agent_ws_server.py connection.ack + stream/unary branches
(agent_ws_server.py:339, :788, :821) in minimal form.
"""
from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import serve

from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.agentserver")

# First frame sent on every new gateway connection (NOT E2A-shaped — a plain
# event frame, exactly like jiuwenclaw agent_ws_server.py:339).
ACK_FRAME = {
    "type": "event",
    "event": EventType.CONNECTION_ACK.value,
    "payload": {"status": "ready"},
}


def _echo_text(env: E2AEnvelope) -> str:
    return "Echo: " + str(env.params.get("query", ""))


async def _echo_stream(ws, env: E2AEnvelope) -> None:
    text = _echo_text(env)
    seq = 0
    for ch in text:
        chunk = E2AResponse(
            request_id=env.request_id,
            sequence=seq,
            is_final=False,
            status="in_progress",
            response_kind="e2a.chunk",
            body={"result": {"content": ch}},
        )
        await ws.send(chunk.model_dump_json())
        seq += 1
        # make streaming visible to the eye
        await asyncio.sleep(0.02)
    final = E2AResponse(
        request_id=env.request_id,
        sequence=seq,
        is_final=True,
        status="succeeded",
        response_kind="e2a.complete",
        body={"result": {"content": text}},
    )
    await ws.send(final.model_dump_json())


async def _echo_unary(ws, env: E2AEnvelope) -> None:
    text = _echo_text(env)
    resp = E2AResponse(
        request_id=env.request_id,
        sequence=0,
        is_final=True,
        status="succeeded",
        response_kind="e2a.complete",
        body={"result": {"content": text}},
        is_stream=False,
    )
    await ws.send(resp.model_dump_json())


async def handler(ws) -> None:
    # greet the gateway so it knows the server is ready
    await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
    async for raw in ws:
        try:
            env = E2AEnvelope.model_validate_json(raw)
        except Exception as exc:  # malformed envelope
            err = E2AResponse(
                request_id="?",
                status="failed",
                response_kind="e2a.error",
                body={"error": str(exc)},
            )
            await ws.send(err.model_dump_json())
            continue
        try:
            if env.is_stream:
                await _echo_stream(ws, env)
            else:
                await _echo_unary(ws, env)
        except Exception as exc:
            log.exception("echo failed for %s: %s", env.request_id, exc)
            err = E2AResponse(
                request_id=env.request_id,
                status="failed",
                response_kind="e2a.error",
                body={"error": str(exc)},
            )
            await ws.send(err.model_dump_json())


async def main() -> None:
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
