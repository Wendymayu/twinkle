"""TeamTaskStore — team 共享任务队列,复用 TodoStore 单例,加编排层。

spec §4:claim 独占(校验 owner 空) + 依赖解除(派生) + 环检测(DFS) + 4 态。
复用 TodoStore 单例(按 team:{sid} 存,leader/member 共享同一队列);
claim 在 TodoStore._lock 内 load→校验→save(spec §4.3 复用 _lock)。
member→leader 求助走 metadata.help_reason(不混 blocked)。
"""
from __future__ import annotations

import time

from twinkle.agentserver.todo import TodoError, TodoTask, get_todo_store


class TeamTaskStore:
    """team session 级编排层,复用 TodoStore 单例。"""

    def __init__(self, team_session_id: str) -> None:
        # team_session_id 形如 "team:{leader_sid}";leader/member 用同一 key
        self._sid = team_session_id

    @property
    def _store(self):
        # 惰性取单例——Team.__init__ 建 TeamTaskStore 时不触发 get_todo_store(),
        # 避免非 team-task 测试(如 test_team.py 的 member 测试,conftest 的 todo
        # fixture 非 autouse)写脏默认 todo 目录。TeamTaskStore 方法调时才取。
        return get_todo_store()

    # ── 内部 helper(复用 TodoStore 的 _lock/_load/_save/_find_by_id,spec §4.3)──

    def _find(self, tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        # _find_by_id 是 TodoStore staticmethod(store.py:107-112),实例可调
        return self._store._find_by_id(tasks, task_id)

    def _met_blocked_by(self, tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        """return the task if it's completed, else None(用于依赖校验)。"""
        t = self._find(tasks, task_id)
        return t if (t is not None and t.status == "completed") else None

    def _has_cycle(self, tasks: list[TodoTask], start_id: str, visited: set[str]) -> bool:
        """DFS 环检测:start_id 沿 blocked_by 走,回到自己则成环。"""
        t = self._find(tasks, start_id)
        if t is None:
            return False
        for dep in t.blocked_by:
            if dep in visited:
                return True
            visited.add(dep)
            if self._has_cycle(tasks, dep, visited):
                return True
            visited.discard(dep)
        return False

    # ── public API ──

    async def create_task(self, subject: str,
                          blocked_by: list[str] | None = None) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            deps = list(blocked_by or [])
            # 环检测:每个新依赖链不能回到自己
            for dep in deps:
                visited = {dep}
                if self._has_cycle(tasks, dep, visited):
                    raise TodoError(f"cycle detected: {dep} dependency chain loops")
            now = time.time()
            task = TodoTask(
                id=f"t-{now}-{len(tasks)}",
                subject=subject,
                status="pending",
                blocked_by=deps,
                created_at=now,
                updated_at=now,
            )
            tasks.append(task)
            self._store._save(self._sid, tasks)
            return task

    async def claim_task(self, task_id: str, member_name: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.status != "pending":
                raise TodoError(f"Task {task_id} not pending (status={t.status}).")
            if t.owner:
                raise TodoError(f"Task {task_id} already claimed by {t.owner}.")
            unmet = [d for d in t.blocked_by if self._met_blocked_by(tasks, d) is None]
            if unmet:
                raise TodoError(f"Task {task_id} blocked by uncompleted: {unmet}")
            t.owner = member_name
            t.status = "in_progress"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def complete_task(self, task_id: str, result: str,
                            member_name: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.owner != member_name:
                raise TodoError(
                    f"Task {task_id} not owned by {member_name} (owner={t.owner}).")
            if t.status != "in_progress":
                raise TodoError(
                    f"Task {task_id} not in_progress (status={t.status}).")
            t.status = "completed"
            t.result = (result or "").strip() or "done"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t
            # 依赖解除是派生:其他 task 的 blocked 在它们 claim 时靠
            # _met_blocked_by 检查(前置 completed 即解除),无需主动改别的 task。

    async def request_help(self, task_id: str, reason: str,
                            member_name: str) -> TodoTask:
        """member 执行中遇困难,在 task 上标 metadata.help_reason(spec §1.4)。

        不改 status(留 in_progress);member run 结束后 release_claims 把它回
        pending、owner 清空,但 metadata 保留——leader 通过 list_tasks/get_task
        看 help_reason 决定是否 steer 或重派。blocked 专属依赖派生,求助不混 blocked。
        """
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.owner != member_name:
                raise TodoError(
                    f"Task {task_id} not owned by {member_name} (owner={t.owner}).")
            t.metadata["help_reason"] = (reason or "").strip() or "unspecified"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def cancel_task(self, task_id: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.status not in ("pending", "in_progress"):
                raise TodoError(
                    f"Task {task_id} cannot be cancelled (status={t.status}).")
            t.status = "cancelled"
            t.owner = ""
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def list_tasks(self, status: str | None = None) -> list[TodoTask]:
        return await self._store.list(self._sid, status=status)

    async def get_task(self, task_id: str) -> TodoTask | None:
        return await self._store.get(self._sid, task_id)

    async def release_claims(self, member_name: str) -> int:
        """member 退出时释放其 claim 但未 complete 的 task(spec §7)。

        owner 清空、status 回 pending;metadata 保留(help_reason 不丢)。
        """
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            now = time.time()
            count = 0
            for t in tasks:
                if t.owner == member_name and t.status == "in_progress":
                    t.owner = ""
                    t.status = "pending"
                    t.updated_at = now
                    count += 1
            if count:
                self._store._save(self._sid, tasks)
            return count
