"""AgentServer — the heavy execution core process.

Phase 1: a `websockets` server that dispatches inbound E2A envelopes to an
AgentLoop (ReAct: think -> tool -> result -> re-decide). Stream-only; no
unary mode. ws_handler(loop, store) lets tests inject a fake loop;
build_agent_loop(store) wires the real config-driven loop for production.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.hooks.base import AgentHook
from twinkle.agentserver.hooks.builtin import LoggingHook
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.sessions import (
    SessionStore, session_store, dispatch_session_rpc, handles_session_rpc,
)
from twinkle.agentserver.tools import tool_manager
from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.agentserver")

ACK_FRAME = {
    "type": "event",
    "event": EventType.CONNECTION_ACK.value,
    "payload": {"status": "ready"},
}


async def _safe_send(ws: ServerConnection, resp: E2AResponse) -> None:
    """Send an E2AResponse; silently swallow ConnectionClosed (client gone)."""
    try:
        await ws.send(resp.model_dump_json())
    except Exception:
        # ConnectionClosedOK / ConnectionClosedError — client disconnected.
        # No point logging at ERROR; this is a normal lifecycle event.
        log.debug("send on closed connection, dropping %s", resp.request_id)


def build_agent_loop(store: SessionStore, hooks: list[AgentHook] | None = None, llm: LLMClient | None = None) -> AgentLoop:
    """Production wiring — config-driven AgentLoop backed by *store*.

    *store* is injected so the caller controls which SessionStore instance
    the loop (chat/ReAct path) and ``ws_handler`` (RPC path) share.
    *hooks* is an optional list of AgentHook instances to register IN
    ADDITION to the always-on PermissionHook (Phase 4). *llm* is an optional
    override (tests inject a scripted client; default = config-driven LLMClient).
    """
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import PermissionHook

    if llm is None:
        llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    tools = tool_manager()
    memory = LongTermMemory()
    engine = permission_engine()
    loop = AgentLoop(llm, store, tools, memory, permission=engine)
    loop.register_hook(PermissionHook(engine))
    if hooks:
        for h in hooks:
            loop.register_hook(h)
    return loop


def ws_handler(loop: AgentLoop, store: SessionStore) -> Callable[[ServerConnection], Awaitable[None]]:
    """Return a ws handler bound to the given AgentLoop + SessionStore.

    Phase 4: concurrent per-request task model so a suspended run_stream
    (awaiting approval) does not block reading the next inbound message
    (approval.respond). Routes ``approval.respond`` to the ApprovalRegistry
    inline; session RPCs inline; everything else spawns a run_stream task,
    one active per session.
    """
    from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY

    async def handler(ws: ServerConnection) -> None:
        try:
            await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
        except Exception:
            return
        send_lock = asyncio.Lock()
        active: dict[str, asyncio.Task] = {}

        async def send(resp: E2AResponse) -> None:
            async with send_lock:
                try:
                    await ws.send(resp.model_dump_json())
                except Exception:
                    log.debug("send on closed connection, dropping %s", resp.request_id)

        async def run_task(envelope: E2AEnvelope) -> None:
            try:
                async for frame in loop.run_stream(envelope):
                    await send(frame)
            except Exception as exc:
                log.exception("agent loop failed for %s: %s", envelope.request_id, exc)
                await send(E2AResponse(
                    request_id=envelope.request_id, is_final=True, status="failed",
                    response_kind="e2a.error", body={"error": str(exc)}))

        try:
            async for raw in ws:
                try:
                    envelope = E2AEnvelope.model_validate_json(raw)
                except Exception as exc:
                    await send(E2AResponse(request_id="?", status="failed",
                        response_kind="e2a.error", body={"error": str(exc)}))
                    continue
                if envelope.method == "approval.respond":
                    await APPROVAL_REGISTRY.handle_respond(envelope, send)
                    continue
                if handles_session_rpc(envelope.method):
                    async for frame in dispatch_session_rpc(envelope, store):
                        await send(frame)
                    continue
                sid = envelope.session_id or envelope.request_id
                cur = active.get(sid)
                if cur is not None and not cur.done():
                    await send(E2AResponse(
                        request_id=envelope.request_id, is_final=True, status="failed",
                        response_kind="e2a.error",
                        body={"error": "a request is already in progress for this session"}))
                    continue
                task = asyncio.create_task(run_task(envelope))
                active[sid] = task
                task.add_done_callback(lambda t, sid=sid: active.pop(sid, None) if active.get(sid) is t else None)
        finally:
            for t in list(active.values()):
                t.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            active.clear()
            APPROVAL_REGISTRY.cancel_all()

    return handler


async def main() -> None:
    store = session_store()
    loop = build_agent_loop(store, hooks=[LoggingHook()])
    handler = ws_handler(loop, store)
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
