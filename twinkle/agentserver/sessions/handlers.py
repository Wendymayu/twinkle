"""AgentServer 的 session/history RPC 派发表。

这些是 Twinkle 原先砍掉的 RPC 方法(``session.list`` /
``history.get`` 在 docs/e2a-introduction.md 标为 "roadmap 不做")——
这里重新采纳,对齐 jiuwenclaw 的远程存储模式:由 agent
server(而非 gateway)持有 session 业务逻辑。

每个 handler yield 单个 ``E2AResponse``,带 ``response_kind="e2a.result"``
和 ``is_final=True``;gateway 把它映射成浏览器的 ``result`` 事件。
失败时 yield 一个 ``status="failed"`` 的 result 帧,body 带 ``error``,
让前端 ``request()`` 能干净地 reject。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from twinkle.agentserver.sessions.store import SessionStore
from twinkle.agentserver.todo import get_todo_store
from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.agentserver.sessions.rpc")

_SESSION_METHODS = {"session.create", "session.list", "session.delete", "history.get", "session.files", "file.read"}


def handles(method: str) -> bool:
    return method in _SESSION_METHODS


async def dispatch_session_rpc(
    envelope: E2AEnvelope, store: SessionStore
) -> AsyncIterator[E2AResponse]:
    method = envelope.method
    session_id = envelope.params.get("session_id") or envelope.session_id
    try:
        if method == "session.create":
            await store.create_session(session_id)
            body = {"type": "session.create", "session_id": session_id}
        elif method == "session.list":
            rows = store.list_sessions()
            body = {"type": "session.list", "sessions": rows}
        elif method == "session.delete":
            await store.delete_session(session_id)
            await get_todo_store().delete(session_id)
            body = {"type": "session.delete", "session_id": session_id}
        elif method == "history.get":
            records = store.get_history(session_id)
            body = {"type": "history.get", "messages": records}
        elif method == "session.files":
            files = store.list_files(session_id)
            body = {"type": "session.files", "files": files}
        elif method == "file.read":
            name = envelope.params.get("name")
            content = store.read_file(session_id, name)
            body = {"type": "file.read", "name": name, "content": content}
        else:
            return  # 不是 session RPC —— 调用方路由给 AgentLoop
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
