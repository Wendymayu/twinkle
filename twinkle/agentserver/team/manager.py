"""TeamManager + Team — session 级 team 生命周期 + member 委派。

Phase 18 对齐 jiuwenswarm：
- TeamManager：全局注册表，session_id → Team（对照 TeamManager._team_agents）
- Team：每 session 一个，管理 member ReActAgent（对照 TeamAgent + build_agent_customizer）
- MEMBER_TOOL_WHITELIST：硬编码 frozenset，所有 member 共享（对照 TOOL_WHITELIST）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.builtin import (
    AuditHook, LoggingHook, MemoryFlushHook, MemoryHook, RepeatToolCallDetectorHook, RetryHook,
    RuntimeEnvHook, SkillHook)
from twinkle.agentserver.team.message_box import MessageBox
from twinkle.agentserver.team.task_store import TeamTaskStore
from twinkle.agentserver.team.workspace import ensure_team_workspace
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config import (
    SUBAGENT_ABORT_TIMEOUT,
    SUBAGENT_MAX_RESULT_CHARS,
    SUBAGENT_SOFT_TIMEOUT,
)

if TYPE_CHECKING:
    from twinkle.agentserver.agent import AgentRequest, ReActAgent
    from twinkle.agentserver.llm_client import LLMClient
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.config.schema import TeamConfig

log = logging.getLogger("twinkle.team")

# ── Tool 白名单 ────────────────────────────────────────────
# 对齐 jiuwenswarm TOOL_WHITELIST（team_runtime_inheritance.py）。
# 所有 member 共享同一集合；差异来自 persona 而非 tool。
# 排除：write_memory、edit_memory（只读 memory）、spawn_subagent
# （不允许递归子派生）、delegate_to_member（不允许递归委派）、
# execute_workflow。

MEMBER_TOOL_WHITELIST: frozenset[str] = frozenset({
    "web_search", "web_fetch",
    "read_file", "write_file", "edit_file", "list_files", "glob",
    "command_exec",
    "memory_search", "read_memory",
    "todo_create", "todo_update", "todo_list", "todo_get",
    "claim_task", "complete_task", "list_tasks", "get_task",   # NEW: member 执行 team task
    "list_skill", "read_skill",
    "cron_list_jobs", "cron_create_job", "cron_update_job",
    "cron_delete_job", "cron_run_now",
})


class Team:
    """每 session 的 team 实例 — 管理 member ReActAgent 和委派。

    对齐 jiuwenswarm TeamAgent：持有 member、处理委派、维护共享 workspace。
    Phase 18 省略：task queue、event bus、member 状态机、Monitor 事件、SQLite 共享状态。
    """

    def __init__(
        self,
        llm: "LLMClient",
        store: "SessionStore",
        parent_tools: ToolManager,
        session_id: str,
        config: "TeamConfig",
    ) -> None:
        self._llm = llm
        self._store = store
        self._parent_tools = parent_tools
        self._session_id = session_id
        self._config = config
        self._members: dict[str, "ReActAgent"] = {}
        self._inboxes: dict[str, MessageBox] = {}    # member_name → MessageBox
        self._personas: dict[str, str] = {}         # member_name → persona (同名冲突校验)
        self.workspace = ensure_team_workspace(session_id)
        self.task_store = TeamTaskStore(f"team:{session_id}")

    # ── member key ──────────────────────────────────────────

    @staticmethod
    def _member_key(member_name: str) -> str:
        """member_name 即 key(spec §3.1:稳定可读,替代 persona hash)。"""
        return member_name

    def _member_session_id(self, member_name: str) -> str:
        return f"{self._session_id}__team_{member_name}"

    # ── member 生命周期 ────────────────────────────────────

    async def _ensure_member(self, member_name: str, persona: str) -> "ReActAgent":
        if member_name in self._members:
            if self._personas[member_name] != persona:
                raise ValueError(
                    f"member_name '{member_name}' already used for a different persona")
            return self._members[member_name]

        member = await self._build_member(member_name, persona)
        self._members[member_name] = member
        self._personas[member_name] = persona
        return member

    async def _build_member(self, member_name: str, persona: str) -> "ReActAgent":
        """为给定 persona 构建定制化的 ReActAgent。

        等价于 jiuwenswarm build_agent_customizer()：
        过滤后的 tool、结构化 team prompt（role → persona → workspace）、
        共享 workspace。
        """
        from twinkle.agentserver.agent import ReActAgent, member_base_sections

        # 1. 按 MEMBER_TOOL_WHITELIST 过滤的 ToolManager
        tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name in MEMBER_TOOL_WHITELIST:
                tm.register(t)

        # 2. Member 身份（persona → workspace → base prompt）焙进 base_sections，
        #    构造时注入；loop 每步重建它。session 不再存储 system msg。
        member_sid = self._member_session_id(member_name)
        await self._store.create_session(member_sid)

        # 3. 构建 ReActAgent — inbox 经构造器接入，使 send_member
        #    （写入 self._inboxes[member_name]）和 run 循环 drain
        #    （读 agent._inbox）看到同一个 MessageBox。
        if member_name not in self._inboxes:
            self._inboxes[member_name] = MessageBox()
        inbox = self._inboxes[member_name]
        hooks = [SkillHook(), MemoryHook(), MemoryFlushHook(llm=self._llm),
                 LoggingHook(), RepeatToolCallDetectorHook(), RetryHook(), RuntimeEnvHook(),
                 AuditHook()]
        return ReActAgent(
            self._llm, self._store, tm,
            hooks=tuple(hooks),
            inbox=inbox,
            base_sections=member_base_sections(
                persona=persona, workspace=str(self.workspace), member_name=member_name),
        )

    # ── 委派 ──────────────────────────────────────────

    async def delegate(self, member_name: str, persona: str,
                       objective: str, prompt: str = "") -> str:
        """按名字委派给 member；首次则构建+启动。运行到收敛。"""
        member = await self._ensure_member(member_name, persona)
        member_sid = self._member_session_id(member_name)
        query = f"{objective}\n\n{prompt}" if prompt else objective

        from twinkle.agentserver.agent import AgentRequest
        request = AgentRequest(
            session_id=member_sid,
            request_id=f"{self._session_id}__team_{uuid.uuid4().hex[:8]}",
            query=query,
        )
        return await self._drive_member(member, request, member_name)

    async def send_member(self, member_name: str, content: str) -> str:
        """Leader → member 单向 steer:投递到 member 信箱,不阻塞。

        member 跑时 run 循环每步 drain;idle 时滞留信箱,下次 delegate 启动时 drain(无害)。
        """
        if member_name not in self._inboxes:
            raise KeyError(f"unknown member: {member_name}")
        self._inboxes[member_name].put(content)
        return f"sent to {member_name}"

    async def _drive_member(self, member: "ReActAgent",
                            request: "AgentRequest",
                            member_name: str = "") -> str:
        """运行 member agent 到收敛；返回最终内容。

        同 SubagentExecutor._drive_child 的模式：用 child task 做
        ContextVar 隔离、queue drain、soft/hard 超时、截断。
        """
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            from twinkle.agentserver.team.context import (
                MEMBER_WORKSPACE, CURRENT_MEMBER_NAME)
            MEMBER_WORKSPACE.set(self.workspace)
            if member_name:
                CURRENT_MEMBER_NAME.set(member_name)
            try:
                async for frame in member.run(request):
                    await queue.put(frame)
            except Exception as exc:
                await queue.put(exc)
            finally:
                await queue.put(None)

        runner = asyncio.create_task(_run())
        final = ""
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=SUBAGENT_SOFT_TIMEOUT)
                except asyncio.TimeoutError:
                    return "[member timeout]"
                if frame is None:
                    break
                if isinstance(frame, Exception):
                    log.warning("member error: %s", frame)
                    return f"[member error: {type(frame).__name__}]"
                if frame.response_kind == "e2a.complete":
                    final = frame.body.get("result", {}).get("content", "") or ""
                elif frame.response_kind == "e2a.error":
                    return f"[member error: {frame.body.get('error', 'unknown')}]"
            if len(final) > SUBAGENT_MAX_RESULT_CHARS:
                final = final[:SUBAGENT_MAX_RESULT_CHARS] + "\n…[truncated]"
            return final
        finally:
            if not runner.done():
                runner.cancel()
            try:
                await asyncio.wait_for(runner, timeout=SUBAGENT_ABORT_TIMEOUT)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            # member run 结束,释放其 claim 但未 complete 的 task(spec §7)
            if member_name:
                try:
                    released = await self.task_store.release_claims(member_name)
                    if released:
                        log.info("released %d claimed task(s) of member %s",
                                 released, member_name)
                except Exception as exc:
                    log.warning("release_claims failed for %s: %s",
                                member_name, exc)

    def cleanup(self) -> None:
        self._members.clear()


class TeamManager:
    """全局单例注册表：session_id → Team。

    对齐 jiuwenswarm TeamManager._team_agents：dict[session_id, TeamAgent]。
    Phase 18 省略：monitor、stream task、evolution rail、分布式 runtime。
    """

    def __init__(
        self,
        llm: "LLMClient",
        store: "SessionStore",
        parent_tools: ToolManager,
        config: "TeamConfig",
    ) -> None:
        self._llm = llm
        self._store = store
        self._parent_tools = parent_tools
        self._config = config
        self._teams: dict[str, Team] = {}

    def ensure_team(self, session_id: str) -> Team:
        """获取或创建某 session 的 Team 实例。"""
        if session_id not in self._teams:
            self._teams[session_id] = Team(
                llm=self._llm,
                store=self._store,
                parent_tools=self._parent_tools,
                session_id=session_id,
                config=self._config,
            )
            log.info("team created: session_id=%s", session_id)
        return self._teams[session_id]

    def destroy_team(self, session_id: str) -> None:
        """销毁某 session 的 Team 实例并释放资源。"""
        team = self._teams.pop(session_id, None)
        if team is not None:
            team.cleanup()
            log.info("team destroyed: session_id=%s", session_id)
