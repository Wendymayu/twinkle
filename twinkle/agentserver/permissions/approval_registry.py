"""ApprovalRegistry — approval_id → asyncio.Future 单例(对齐 TodoStore)。

agent_loop 在 ASK 时 register(approval_id) 拿 Future 并 await;ws_handler
收到 approval.respond 时 handle_respond() resolve Future + 回 e2a.result ack。
Future 用 approval_id 做 key(不是 request_id),使 approval.respond(R2) 能
找到挂起的原始 chat 流(R)。详见 spec §9。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.permissions.approval")


class ApprovalRegistry:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def register(self, approval_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._futures[approval_id] = fut
        return fut

    def resolve(self, approval_id: str, decision: str) -> bool:
        fut = self._futures.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    async def handle_respond(
        self,
        envelope: E2AEnvelope,
        send: Callable[[E2AResponse], Awaitable[None]],
    ) -> None:
        approval_id = envelope.params.get("approval_id")
        decision = envelope.params.get("decision")
        ok = self.resolve(approval_id, decision) if (approval_id and decision) else False
        if not ok:
            log.warning("approval.respond rejected: approval_id=%r decision=%r", approval_id, decision)
        ack = E2AResponse(
            request_id=envelope.request_id, sequence=0, is_final=True,
            status="succeeded" if ok else "failed",
            response_kind="e2a.result",
            body={"type": "approval.respond", "approval_id": approval_id,
                  "accepted": ok} if ok else
                 {"type": "approval.respond", "approval_id": approval_id,
                  "accepted": False, "error": "unknown or expired approval_id"},
        )
        await send(ack)
        if approval_id and ok:
            self._futures.pop(approval_id, None)

    def cancel_all(self) -> None:
        for fut in list(self._futures.values()):
            if not fut.done():
                fut.cancel()
        self._futures.clear()


# 模块级单例
APPROVAL_REGISTRY = ApprovalRegistry()
