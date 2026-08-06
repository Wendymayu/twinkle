"""TeamManager + Team — session-scoped team lifecycle + member delegation.

Phase A alignment with jiuwenswarm:
- TeamManager: global registry, session_id → Team (cf. TeamManager._team_agents)
- Team: per-session, manages member ReActAgents (cf. TeamAgent + build_agent_customizer)
- MEMBER_TOOL_WHITELIST: hardcoded frozenset, all members share (cf. TOOL_WHITELIST)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.builtin import LoggingHook, MemoryHook, RetryHook, SkillHook
from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.workspace import ensure_team_workspace, team_workspace_dir
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config import (
    SUBAGENT_ABORT_TIMEOUT,
    SUBAGENT_HARD_TIMEOUT,
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
    "list_skill", "read_skill",
    "cron_list_jobs", "cron_create_job", "cron_update_job",
    "cron_delete_job", "cron_run_now",
})


class Team:
    """Per-session team instance — manages member ReActAgents and delegation.

    Aligns with jiuwenswarm TeamAgent: holds members, handles delegation,
    maintains shared workspace. Phase A omits: task queue, event bus,
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
        self.workspace = ensure_team_workspace(session_id)

    # ── member key ──────────────────────────────────────────

    @staticmethod
    def _member_key(persona: str) -> str:
        """Stable, cross-process deterministic key from persona string."""
        return hashlib.blake2b(persona.encode(), digest_size=8).hexdigest()

    def _member_session_id(self, persona: str) -> str:
        return f"{self._session_id}__team_{self._member_key(persona)}"

    # ── member lifecycle ────────────────────────────────────

    async def _get_or_create_member(self, persona: str) -> "ReActAgent":
        key = self._member_key(persona)
        if key in self._members:
            return self._members[key]

        member = await self._build_member(persona)
        self._members[key] = member
        return member

    async def _build_member(self, persona: str) -> "ReActAgent":
        """Build a ReActAgent customized for the given persona.

        Equivalent to jiuwenswarm build_agent_customizer():
        filtered tools, structured team prompt (role → persona → workspace),
        shared workspace.
        """
        from twinkle.agentserver.agent import ReActAgent, build_member_system_prompt

        # 1. ToolManager filtered by MEMBER_TOOL_WHITELIST
        tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name in MEMBER_TOOL_WHITELIST:
                tm.register(t)

        # 2. Pre-seed session with structured team system prompt
        #    Sections: team_role → persona → workspace → base prompt
        member_sid = self._member_session_id(persona)
        await self._store.create_session(member_sid)
        system_prompt = build_member_system_prompt(
            persona=persona,
            workspace=str(self.workspace),
        )
        await self._store.append(member_sid, {"role": "system", "content": system_prompt})

        # 3. Build ReActAgent
        hooks = [SkillHook(), MemoryHook(), LoggingHook(), RetryHook()]
        return ReActAgent(
            self._llm, self._store, tm,
            hooks=tuple(hooks),
            max_steps=SUBAGENT_MAX_STEPS,
        )

    # ── delegation ──────────────────────────────────────────

    async def delegate(self, persona: str, objective: str,
                       prompt: str = "") -> str:
        """Delegate a task to a member, run to convergence, return final content."""
        member = await self._get_or_create_member(persona)
        member_sid = self._member_session_id(persona)
        query = f"{objective}\n\n{prompt}" if prompt else objective

        from twinkle.agentserver.agent import AgentRequest
        request = AgentRequest(
            session_id=member_sid,
            request_id=f"{self._session_id}__team_{uuid.uuid4().hex[:8]}",
            query=query,
        )
        return await self._drive_member(member, request)

    async def _drive_member(self, member: "ReActAgent",
                            request: "AgentRequest") -> str:
        """Run member agent to convergence; return final content.

        Same pattern as SubagentExecutor._drive_child: child task for
        ContextVar isolation, queue drain, soft/hard timeouts, truncation.
        """
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            from twinkle.agentserver.team.context import MEMBER_WORKSPACE
            MEMBER_WORKSPACE.set(self.workspace)
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

    def cleanup(self) -> None:
        self._members.clear()


class TeamManager:
    """Global singleton registry: session_id → Team.

    Aligns with jiuwenswarm TeamManager._team_agents: dict[session_id, TeamAgent].
    Phase A omits: monitors, stream tasks, evolution rails, distributed runtime.
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

    def get_or_create_team(self, session_id: str) -> Team:
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
