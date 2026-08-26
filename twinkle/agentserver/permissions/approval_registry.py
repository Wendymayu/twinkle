"""ApprovalRegistry — approval_id → asyncio.Future 单例(对齐 TodoStore)。

agent_loop 在 ASK 时 register(approval_id) 拿 Future 并 await;ws_handler
收到 approval.respond 时 handle_respond() resolve Future + 回 e2a.result ack。
Future 用 approval_id 做 key(不是 request_id),使 approval.respond(R2) 能
找到挂起的原始 chat 流(R)。详见 spec §9。

Phase 10: 审批状态持久化 — ASK 时 save_pending() 写磁盘,resolve 后
clear_pending() 清除,重连时 get_pending() 读取,让浏览器断连后能恢复审批卡片。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from twinkle.e2a.models import E2AEnvelope, E2AResponse

log = logging.getLogger("twinkle.permissions.approval")


@dataclass
class ApprovalPendingRecord:
    """审批中断时持久化的元数据,用于浏览器重连后恢复审批卡片。"""

    approval_id: str
    tool: str
    args: dict[str, Any]
    tool_call_id: str
    reason: str
    request_id: str
    session_id: str
    created_at: float


class ApprovalRegistry:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    # --- 持久化 helper ---

    def _pending_path(self, session_id: str) -> Path:
        """返回该 session 的 pending-approval 文件路径。"""
        from twinkle.config import SESSIONS_DIR

        return Path(SESSIONS_DIR) / session_id / ".approval_pending.json"

    def _read_pending_file(self, session_id: str) -> list[dict]:
        """从磁盘读取 pending approval。文件缺失/损坏时返回 []。"""
        path = self._pending_path(session_id)
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("corrupt .approval_pending.json for session %s, ignoring", session_id)
            return []

    def _write_pending_file(self, session_id: str, records: list[dict]) -> None:
        """原子性地把 pending approval 写入磁盘(.tmp + os.replace)。"""
        path = self._pending_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        try:
            temp_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            os.replace(temp_path, path)
        except Exception:
            log.exception("failed to write .approval_pending.json for session %s", session_id)
            temp_path.unlink(missing_ok=True)

    def save_pending(self, session_id: str, record: ApprovalPendingRecord) -> None:
        """往磁盘追加一条 pending approval 记录。"""
        records = self._read_pending_file(session_id)
        records.append(asdict(record))
        self._write_pending_file(session_id, records)

    def clear_pending(self, session_id: str, approval_id: str) -> None:
        """从磁盘移除指定 pending approval。"""
        records = self._read_pending_file(session_id)
        remaining = [r for r in records if r.get("approval_id") != approval_id]
        if not remaining:
            # 为空时直接删除文件
            path = self._pending_path(session_id)
            path.unlink(missing_ok=True)
        else:
            self._write_pending_file(session_id, remaining)

    def get_pending(self, session_id: str) -> list[dict]:
        """读取某 session 的 pending approval。供 approval.check_pending RPC 使用。"""
        return self._read_pending_file(session_id)

    def clear_all_pending(self, session_id: str) -> None:
        """移除某 session 的全部 pending approval。run_stream finally 里的安全网。"""
        path = self._pending_path(session_id)
        path.unlink(missing_ok=True)

    # --- 内存态 Future 管理(不变) ---

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
            # 成功 resolve 后清除持久化状态
            session_id = envelope.params.get("session_id") or envelope.session_id
            if session_id:
                self.clear_pending(session_id, approval_id)

    def cancel_all(self) -> None:
        for fut in list(self._futures.values()):
            if not fut.done():
                fut.cancel()
        self._futures.clear()


# 模块级单例
APPROVAL_REGISTRY = ApprovalRegistry()
