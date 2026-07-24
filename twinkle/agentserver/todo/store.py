# twinkle/agentserver/todo/store.py
"""TodoStore — agent 内部任务规划的内存存储。

dict[session_id, list[TodoTask]] + 每 session 一把 asyncio.Lock,
串行化 read-modify-write 防丢更新。对齐 jiuwenclaw
agentserver/tools/todo_toolkits.py 的 TodoToolkit,但:
- 存内存(不写 todo.md),与 Twinkle SessionStore 哲学一致;
- 只保留 create/complete/list_tasks(砍 start/insert/remove/batch);
- 砍 op-result 发布总线(Twinkle 无 rail 消费)。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class TodoTask:
    idx: int
    title: str
    status: str  # "waiting" | "running" | "completed"
    result: str = ""


class TodoError(Exception):
    """业务级错误,消息可直接回给模型。"""


class TodoStore:
    def __init__(self) -> None:
        self._data: dict[str, list[TodoTask]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        # 单线程事件循环下 setdefault 无竞态(同步调用,无 await 间隙)。
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def create(self, session_id: str, tasks: list[str]) -> list[TodoTask]:
        if not tasks:
            raise TodoError("tasks must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._data.get(session_id, [])
            if existing:
                raise TodoError(
                    f"todo list already exists for session {session_id}."
                )
            new = [
                TodoTask(idx=i + 1, title=t, status="waiting", result="")
                for i, t in enumerate(tasks)
            ]
            self._data[session_id] = new
            return list(new)

    async def complete(
        self, session_id: str, idx: int, result: str = ""
    ) -> list[TodoTask]:
        async with self._lock(session_id):
            tasks = self._data.get(session_id, [])
            for t in tasks:
                if t.idx == idx:
                    if t.status == "completed":
                        raise TodoError(f"Task {idx} is already completed.")
                    t.status = "completed"
                    t.result = (result or "").strip() or "done"
                    return list(tasks)
            raise TodoError(f"Task {idx} not found.")

    async def list_tasks(self, session_id: str) -> list[TodoTask]:
        async with self._lock(session_id):
            return list(self._data.get(session_id, []))
