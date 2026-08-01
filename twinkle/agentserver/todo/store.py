# twinkle/agentserver/todo/store.py
"""TodoStore — agent 内部任务规划的磁盘持久化存储。

per-session flat 文件 <todos_dir>/<session_id>.json, 每次操作 load→改→save,
跨进程重启存活。TodoTask 数据模型增强：id(UUID)、subject、description、
blocked_by、owner、metadata、created_at/updated_at。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger("twinkle.agentserver.todo.store")

_VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


@dataclasses.dataclass
class TodoTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    result: str = ""
    blocked_by: list[str] = dataclasses.field(default_factory=list)
    owner: str = ""
    metadata: dict = dataclasses.field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


class TodoError(Exception):
    """业务级错误, 消息可直接回给模型。"""


class TodoStore:
    def __init__(self, todos_dir: str | Path) -> None:
        self._root = Path(todos_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # --- paths & locks ---

    def _todo_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # --- I/O ---

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
    def _record_to_task(rec: dict) -> "TodoTask | None":
        try:
            return TodoTask(
                id=str(rec["id"]),
                subject=str(rec["subject"]),
                description=str(rec.get("description", "")),
                status=str(rec.get("status", "pending")),
                result=str(rec.get("result", "")),
                blocked_by=list(rec.get("blocked_by", [])),
                owner=str(rec.get("owner", "")),
                metadata=dict(rec.get("metadata", {})),
                created_at=float(rec.get("created_at", 0.0)),
                updated_at=float(rec.get("updated_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _find_by_id(tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        for t in tasks:
            if t.id == task_id:
                return t
        return None

    # --- public API ---

    async def create(
        self,
        session_id: str,
        subjects: list[str],
        sequential: bool = False,
    ) -> list[TodoTask]:
        """Create tasks for the session. Raises TodoError if subjects is empty,
        or if tasks already exist for this session (guard against clobbering)."""
        if not subjects:
            raise TodoError("subjects must be a non-empty list.")
        async with self._lock(session_id):
            existing = self._load(session_id)
            if existing:
                raise TodoError(
                    f"todo list already exists for session {session_id}."
                )
            now = time.time()
            tasks = [
                TodoTask(
                    id=str(uuid.uuid4()),
                    subject=s,
                    created_at=now,
                    updated_at=now,
                )
                for s in subjects
            ]
            if sequential:
                for i, t in enumerate(tasks):
                    if i > 0:
                        t.blocked_by = [tasks[i - 1].id]
            self._save(session_id, tasks)
            return tasks

    async def update(
        self,
        session_id: str,
        task_id: str,
        *,
        status: str | None = None,
        result: str | None = None,
        owner: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[TodoTask, str | None]:
        """Update a task. Returns (task, warning) where warning is None on
        success or a string when blocked_by dependencies are unresolved.
        Raises TodoError if task_id not found."""
        async with self._lock(session_id):
            tasks = self._load(session_id)
            task = self._find_by_id(tasks, task_id)
            if task is None:
                raise TodoError(f"Task {task_id} not found.")
            now = time.time()
            warning = None
            if status is not None:
                if status not in _VALID_STATUSES:
                    raise TodoError(
                        f"Invalid status '{status}'. Must be one of {sorted(_VALID_STATUSES)}."
                    )
                # Guard: check blocked_by when transitioning to in_progress
                if status == "in_progress" and task.blocked_by:
                    unresolved = [
                        bid
                        for bid in task.blocked_by
                        if self._find_by_id(tasks, bid) is None
                        or self._find_by_id(tasks, bid).status != "completed"
                    ]
                    if unresolved:
                        warning = (
                            f"Warning: task {task_id} has unresolved "
                            f"dependencies: {unresolved}"
                        )
                task.status = status
            if result is not None:
                task.result = (result or "").strip() or "done"
            if owner is not None:
                task.owner = owner
            if metadata is not None:
                # merge-style: update keys, delete keys with None value
                for k, v in metadata.items():
                    if v is None:
                        task.metadata.pop(k, None)
                    else:
                        task.metadata[k] = v
            task.updated_at = now
            self._save(session_id, tasks)
            return task, warning

    async def list(
        self,
        session_id: str,
        status: str | None = None,
    ) -> list[TodoTask]:
        async with self._lock(session_id):
            tasks = self._load(session_id)
            if status is not None:
                tasks = [t for t in tasks if t.status == status]
            return tasks

    async def get(
        self,
        session_id: str,
        task_id: str,
    ) -> TodoTask | None:
        async with self._lock(session_id):
            return self._find_by_id(self._load(session_id), task_id)

    async def delete(self, session_id: str) -> bool:
        """Remove the session's todo file. Returns False if absent."""
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
