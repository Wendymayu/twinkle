"""TeamManager + Team — session-scoped team lifecycle + member delegation.

Phase 18 alignment with jiuwenswarm:
- TeamManager: global registry, session_id → Team (cf. TeamManager._team_agents)
- Team: per-session, manages member ReActAgents (cf. TeamAgent + build_agent_customizer)
- MEMBER_TOOL_WHITELIST: hardcoded frozenset, all members share (cf. TOOL_WHITELIST)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.builtin import (
    LoggingHook, MemoryFlushHook, MemoryHook, RetryHook, RuntimeEnvHook, SkillHook)
from twinkle.agentserver.team.message_box import MessageBox
from twinkle.agentserver.team.task_store import TeamTaskStore
from twinkle.agentserver.team.workspace import ensure_team_workspace
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config import (
    SUBAGENT_ABORT_TIMEOUT,
    SUBAGENT_MAX_RESULT_CHARS,
    SUBAGENT_MAX_STEPS,
    SUBAGENT_SOFT_TIMEOUT,
)

if TYPE_CHECKING:
    from twinkle.agentserver.agent import AgentRequest, ReActAgent
    from twinkle.agentserver.llm_client import LLMClient
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.config.schema import TeamConfig

log = logging.getLogger("twinkle.team")

# ── Tool whitelist ────────────────────────────────────────────
# Aligns with jiuwenswarm TOOL_WHITELIST (team_runtime_inheritance.py).
# All members share the same set; differences come from persona, not tools.
# Excludes: write_memory, edit_memory (read-only memory), spawn_subagent
# (no recursive sub-spawning), delegate_to_member (no recursive delegation),
# execute_workflow.

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
    """Per-session team instance — manages member ReActAgents and delegation.

    Aligns with jiuwenswarm TeamAgent: holds members, handles delegation,
    maintains shared workspace. Phase 18 omits: task queue, event bus,
    member state machine, Monitor events, SQLite shared state.
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

    # ── member lifecycle ────────────────────────────────────

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
        """Build a ReActAgent customized for the given persona.

        Equivalent to jiuwenswarm build_agent_customizer():
        filtered tools, structured team prompt (role → persona → workspace),
        shared workspace.
        """
        from twinkle.agentserver.agent import ReActAgent, member_base_sections

        # 1. ToolManager filtered by MEMBER_TOOL_WHITELIST
        tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name in MEMBER_TOOL_WHITELIST:
                tm.register(t)

        # 2. Member identity (persona → workspace → base prompt) baked into base_sections,
        #    injected at construction; loop rebuilds it each step. Session no longer stores a system msg.
        member_sid = self._member_session_id(member_name)
        await self._store.create_session(member_sid)

        # 3. Build ReActAgent — inbox wired via constructor so send_member
        #    (writes to self._inboxes[member_name]) and the run-loop drain
        #    (reads agent._inbox) see the same MessageBox.
        if member_name not in self._inboxes:
            self._inboxes[member_name] = MessageBox()
        inbox = self._inboxes[member_name]
        hooks = [SkillHook(), MemoryHook(), MemoryFlushHook(llm=self._llm),
                 LoggingHook(), RetryHook(), RuntimeEnvHook()]
        return ReActAgent(
            self._llm, self._store, tm,
            hooks=tuple(hooks),
            max_steps=SUBAGENT_MAX_STEPS,
            inbox=inbox,
            base_sections=member_base_sections(
                persona=persona, workspace=str(self.workspace), member_name=member_name),
        )

    # ── delegation ──────────────────────────────────────────

    async def delegate(self, member_name: str, persona: str,
                       objective: str, prompt: str = "") -> str:
        """Delegate to a member by name; builds+starts if first time. Run to convergence."""
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
        """Run member agent to convergence; return final content.

        Same pattern as SubagentExecutor._drive_child: child task for
        ContextVar isolation, queue drain, soft/hard timeouts, truncation.
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
    """Global singleton registry: session_id → Team.

    Aligns with jiuwenswarm TeamManager._team_agents: dict[session_id, TeamAgent].
    Phase 18 omits: monitors, stream tasks, evolution rails, distributed runtime.
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
        """Get or create the Team instance for a session."""
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
        """Destroy a session's Team instance and release resources."""
        team = self._teams.pop(session_id, None)
        if team is not None:
            team.cleanup()
            log.info("team destroyed: session_id=%s", session_id)
