"""AgentServer — the heavy execution core process.

Phase 1: a `websockets` server that dispatches inbound E2A envelopes to an
AgentLoop (ReAct: think -> tool -> result -> re-decide). Stream-only; no
unary mode. ws_handler(loop, store) lets tests inject a fake loop;
agent_loop() wires the real config-driven loop for production.
"""
from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_rpc import dispatch_session_rpc, handles as handles_session_rpc
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools import tool_manager
from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, SESSIONS_DIR
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


def build_agent_loop():
    """Production wiring — config-driven LLM + disk-backed SessionStore.

    Returns ``(loop, store)`` so the caller can share ONE store instance
    between the AgentLoop (chat/reagent path) and ``ws_handler`` (RPC path),
    mirroring jiuwenclaw's remote storage mode where both flows see the same
    sessions.
    """
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    store = SessionStore(SESSIONS_DIR)
    tools = tool_manager()
    memory = LongTermMemory()
    return AgentLoop(llm, store, tools, memory), store


def agent_loop() -> AgentLoop:
    """Thin shim kept for any existing one-arg caller."""
    loop, _ = build_agent_loop()
    return loop


def ws_handler(loop: AgentLoop, store: SessionStore):
    """Return a ws handler bound to the given AgentLoop + SessionStore.

    Routes ``session.*/history.get`` envelopes to ``dispatch_session_rpc``
    (single ``e2a.result`` frame per RPC); everything else falls through to
    ``loop.run_stream`` (the ReAct chat path). Both paths share ``store``.
    """

    async def handler(ws) -> None:
        try:
            await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
        except Exception:
            return  # client closed before we even greeted
        async for raw in ws:
            try:
                envelope = E2AEnvelope.model_validate_json(raw)
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
                if handles_session_rpc(envelope.method):
                    async for frame in dispatch_session_rpc(envelope, store):
                        await _safe_send(ws, frame)
                else:
                    async for frame in loop.run_stream(envelope):
                        await _safe_send(ws, frame)
            except Exception as exc:
                log.exception("agent loop failed for %s: %s", envelope.request_id, exc)
                err = E2AResponse(
                    request_id=envelope.request_id,
                    status="failed",
                    response_kind="e2a.error",
                    body={"error": str(exc)},
                )
                await _safe_send(ws, err)

    return handler


async def main() -> None:
    loop, store = build_agent_loop()
    handler = ws_handler(loop, store)
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
