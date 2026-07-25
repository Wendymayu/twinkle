# twinkle/agentserver/todo/store.py
"""TodoStore — agent 内部任务规划的磁盘持久化存储。

per-session flat 文件 <todos_dir>/<session_id>.json,每次操作 load→改→save,
跨进程重启存活。对齐 jiuwenswarm TodoToolkit(todo_toolkits.py)的机制,沿用
Twinkle SessionStore 的 JSON/async 约定:
- 存 JSON(dataclass asdict 精确往返),不存 markdown;
- 无内存缓存,每次 op 都读写盘(数据极小,操作稀疏);
- per-session asyncio.Lock 串行化 read-modify-write;
- 文件缺失/损坏 → _load 返 [] 不抛(对齐 SessionStore 坏行跳过);
- 写盘 OSError → 包 TodoError,让工具层回错给模型,不炸 run_stream。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path

log = logging.getLogger("twinkle.agentserver.todo.store")


@dataclasses.dataclass
class TodoTask:
    idx: int
    title: str
    status: str  # "waiting" | "running" | "completed"
    result: str = ""


class TodoError(Exception):
    """业务级错误,消息可直接回给模型。"""


class TodoStore:
    def __init__(self, todos_dir: str | Path) -> None:
        self._root = Path(todos_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # --- paths & locks ---

    def _todo_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _lock(self, session_id: str) -> asyncio.Lock:
        # 单线程事件循环下 setdefault 无竞态(同步调用,无 await 间隙)。
        return self._locks.setdefault(session_id, asyncio.Lock())

    # --- I/O (load→mutate→save per op, no cache) ---

    def _load(self, session_id: str) -> list[TodoTask]:
        p = self._todo_path(session_id)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping corrupt todo file %s: %s", session_id, exc)
            return []
        if not isinstance(data, list):
            return []
        out: list[TodoTask] = []
        for rec in data:
            t = self._record_to_task(rec)
            if t is not None:
                out.append(t)
        return out

    def _save(self, session_id: str, tasks: list[TodoTask]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._todo_path(session_id).write_text(
                json.dumps(
                    [dataclasses.asdict(t) for t in tasks],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise TodoError(f"failed to persist todo: {exc}") from exc

    @staticmethod
    def _record_to_task(rec) -> "TodoTask | None":
        try:
            return TodoTask(
                idx=int(rec["idx"]),
                title=str(rec["title"]),
                status=str(rec["status"]),
                result=str(rec.get("result", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # --- public API ---

    async def create(self, session_id: str, tasks: list[str]) -> None:
        """Create a todo list for the session. Raises TodoError if tasks is
        empty, or if a list already exists with any non-completed task (guard
        against clobbering an in-progress plan). Replacing a list whose tasks
        are ALL completed is allowed (fresh plan after the previous finished).
        """
        if not tasks:
            raise TodoError("tasks must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._load(session_id)
            if existing and any(t.status != "completed" for t in existing):
                raise TodoError(
                    f"todo list already in progress for session {session_id}."
                )
            new = [
                TodoTask(idx=i + 1, title=t, status="waiting", result="")
                for i, t in enumerate(tasks)
            ]
            self._save(session_id, new)

    async def complete(
        self, session_id: str, idx: int, result: str = ""
    ) -> None:
        """Mark a task as completed. Raises TodoError if idx not found or
        the task is already completed."""
        async with self._lock(session_id):
            tasks = self._load(session_id)
            for t in tasks:
                if t.idx == idx:
                    if t.status == "completed":
                        raise TodoError(f"Task {idx} is already completed.")
                    t.status = "completed"
                    t.result = (result or "").strip() or "done"
                    self._save(session_id, tasks)
                    return
            raise TodoError(f"Task {idx} not found.")

    async def list_tasks(self, session_id: str) -> list[TodoTask]:
        async with self._lock(session_id):
            return list(self._load(session_id))

    async def delete(self, session_id: str) -> bool:
        """Remove the session's todo file. Returns False if absent. Holds the
        per-session lock so a concurrent create/complete can't recreate the
        file mid-delete (orphan). Called by the session.delete RPC."""
        async with self._lock(session_id):
            p = self._todo_path(session_id)
            if not p.is_file():
                return False
            try:
                p.unlink()
            except OSError as exc:
                log.warning("todo delete failed for %s: %s", session_id, exc)
                return False
            return True
