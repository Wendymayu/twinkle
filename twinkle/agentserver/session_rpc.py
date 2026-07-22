"""Dispatch table for session/history RPCs at the AgentServer.

These are the RPC methods Twinkle originally dropped (``session.list`` /
``history.get`` were marked "roadmap 不做" in docs/e2a-introduction.md) —
re-adopted here, mirroring jiuwenclaw's remote storage mode where the agent
server (not the gateway) owns session business logic.

Each handler yields a single ``E2AResponse`` with ``response_kind="e2a.result"``
and ``is_final=True``; the gateway maps that to the browser ``result`` event.
On failure it yields a ``status="failed"`` result frame with an ``error`` body
so the frontend ``request()`` can reject cleanly.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from twinkle.agentserver.session_store import SessionStore
from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.agentserver.session_rpc")

_SESSION_METHODS = {"session.create", "session.list", "session.delete", "history.get"}


def handles(method: str) -> bool:
    return method in _SESSION_METHODS


async def dispatch_session_rpc(
    envelope: E2AEnvelope, store: SessionStore
) -> AsyncIterator[E2AResponse]:
    method = envelope.method
    sid = envelope.params.get("session_id") or envelope.session_id
    try:
        if method == "session.create":
            await store.create_session(sid)
            body = {"type": "session.create", "session_id": sid}
        elif method == "session.list":
            rows = store.list_sessions()
            body = {"type": "session.list", "sessions": rows}
        elif method == "session.delete":
            await store.delete_session(sid)
            body = {"type": "session.delete", "session_id": sid}
        elif method == "history.get":
            records = store.get_history(sid)
            body = {"type": "history.get", "messages": records}
        else:
            return  # not a session RPC — caller routes to AgentLoop
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=0,
            is_final=True,
            status="succeeded",
            response_kind="e2a.result",
            body=body,
        )
    except Exception as exc:
        log.exception("session rpc %s failed: %s", method, exc)
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=0,
            is_final=True,
            status="failed",
            response_kind="e2a.result",
            body={"type": method, "error": str(exc)},
        )
