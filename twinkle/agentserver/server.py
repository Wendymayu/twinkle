"""AgentServer — the heavy execution core process.

Phase 1: a `websockets` server that dispatches inbound E2A envelopes to an
AgentLoop (ReAct: think -> tool -> result -> re-decide). Stream-only; no
unary mode. make_handler(loop) lets tests inject a fake loop;
build_default_loop() wires the real config-driven loop for production.
"""
from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools import build_default_registry
from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.agentserver")

ACK_FRAME = {
    "type": "event",
    "event": EventType.CONNECTION_ACK.value,
    "payload": {"status": "ready"},
}


async def _safe_send(ws, resp: E2AResponse) -> None:
    """Send an E2AResponse; silently swallow ConnectionClosed (client gone)."""
    try:
        await ws.send(resp.model_dump_json())
    except Exception:
        # ConnectionClosedOK / ConnectionClosedError — client disconnected.
        # No point logging at ERROR; this is a normal lifecycle event.
        log.debug("send on closed connection, dropping %s", resp.request_id)


def build_default_loop() -> AgentLoop:
    """Production wiring — config-driven LLM + default tool registry."""
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    store = SessionStore()
    tools = build_default_registry()
    memory = LongTermMemory()
    return AgentLoop(llm, store, tools, memory)


def make_handler(loop: AgentLoop):
    """Return a ws handler bound to the given AgentLoop."""

    async def handler(ws) -> None:
        try:
            await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
        except Exception:
            return  # client closed before we even greeted
        async for raw in ws:
            try:
                env = E2AEnvelope.model_validate_json(raw)
            except Exception as exc:
                err = E2AResponse(
                    request_id="?",
                    status="failed",
                    response_kind="e2a.error",
                    body={"error": str(exc)},
                )
                await _safe_send(ws, err)
                continue
            try:
                async for frame in loop.run_stream(env):
                    await _safe_send(ws, frame)
            except Exception as exc:
                log.exception("agent loop failed for %s: %s", env.request_id, exc)
                err = E2AResponse(
                    request_id=env.request_id,
                    status="failed",
                    response_kind="e2a.error",
                    body={"error": str(exc)},
                )
                await _safe_send(ws, err)

    return handler


async def main() -> None:
    h = make_handler(build_default_loop())
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(h, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
