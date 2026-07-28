"""SubagentExecutor — builds + runs an isolated child AgentLoop (black-box).

execute_spawn (Task 7): fresh child session, trimmed ToolManager (no
spawn_subagent / memory-writes), reused LLMClient/SessionStore, tighter
max_steps; runs child run_stream in a child asyncio task (ContextVar
isolation) with soft/hard timeouts; returns the child's e2a.complete content
as a SubagentResult.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from twinkle.agentserver.agent_loop import AgentLoop, build_system_prompt
from twinkle.agentserver.hooks.builtin import LoggingHook, MemoryHook, SkillHook
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.subagent_executor.models import (
    EXCLUDED_TOOLS,
    SoftTimeoutError,
    SubagentResult,
    SubagentTaskSpec,
)
from twinkle.e2a.models import E2AEnvelope

if TYPE_CHECKING:
    from twinkle.agentserver.hooks.base import AgentHook
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.config.schema import SubagentConfig

log = logging.getLogger("twinkle.subagent")

_SUBAGENT_ADDENDUM = """\
---

# 子 agent 角色补充 (sub-agent role)

你是被父 agent 委派的隔离子 agent (isolated sub-agent)，执行一个聚焦子任务。

- 你看不到父 agent 的对话历史。你所需的一切在 user 消息（objective）里；缺东西就尽力而为，不要反问用户（你无直连通道）。
- 你有父 agent 的工具，但除外 spawn_subagent（不可再委派）和 write_memory/edit_memory（记忆只读；用 memory_search/read_memory）。
- 可用 skill：调 list_skill 看清单，read_skill(name, "SKILL.md") 载入指令体。
- 用 ReAct loop 完成子任务；完成后把最终答案作为最终消息返回（该答案会回灌给父 agent）。
- 聚焦、简洁——你的输出会成为父 agent 的 tool_result。

子任务角色：{role_id}
"""


class SubagentExecutor:
    def __init__(
        self,
        llm: LLMClient | None,
        store: "SessionStore",
        parent_tools: ToolManager,
        config: "SubagentConfig",
        child_hooks: list["AgentHook"] | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._parent_tools = parent_tools
        self._config = config
        self._child_hooks = child_hooks        # None -> default fresh list per child
        self._active: dict[str, asyncio.Task] = {}

    # --- pure helpers (Task 6) ---

    def _build_child_tool_manager(self) -> ToolManager:
        child_tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name not in EXCLUDED_TOOLS:
                child_tm.register(t)
        return child_tm

    def _child_system_prompt(self, task: SubagentTaskSpec) -> str:
        # Reuse the parent's base prompt (identity + runtime + workspace + tool
        # guidance) so the child uses command_exec/file tools correctly, then
        # append the sub-agent role addendum. Pre-seeding this as the first
        # message also makes _inner_run_stream skip its default build_system_prompt() seed.
        return build_system_prompt() + "\n\n" + _SUBAGENT_ADDENDUM.format(role_id=task.role_id)

    def _build_query(self, task: SubagentTaskSpec) -> str:
        if task.prompt:
            return f"{task.objective}\n\n{task.prompt}"
        return task.objective

    def _resolve_llm(self, model_name: str) -> LLMClient:
        name = (model_name or self._config.model or "").strip()
        if not name:
            return self._llm  # type: ignore[return-value]
        from twinkle.config import LLM_API_KEY, LLM_BASE_URL
        return LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=name)

    def _child_hook_list(self) -> list["AgentHook"]:
        if self._child_hooks is not None:
            return self._child_hooks
        return [SkillHook(), MemoryHook(), LoggingHook()]

    # --- build + run (Task 7) ---

    def _build_child_loop(self, llm: LLMClient) -> AgentLoop:
        child_tm = self._build_child_tool_manager()
        child_loop = AgentLoop(llm, self._store, child_tm, max_steps=self._config.max_steps)
        for hook in self._child_hook_list():
            child_loop.register_hook(hook)
        return child_loop

    async def _drive_child(self, child_loop: AgentLoop, child_env: E2AEnvelope) -> str:
        """Run child run_stream in a child task (ContextVar isolation); drain
        frames via a queue; return the e2a.complete content. Black-box: chunk /
        todo_update frames are discarded."""
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            try:
                async for frame in child_loop.run_stream(child_env):
                    await queue.put(frame)
            except Exception as exc:       # child raised -> forward as a frame
                await queue.put(exc)
            finally:
                await queue.put(None)       # sentinel

        runner = asyncio.create_task(_run())   # context copy -> child's ContextVar.set don't leak
        final = ""
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=self._config.soft_timeout)
                except asyncio.TimeoutError:
                    raise SoftTimeoutError(
                        f"no child activity for {self._config.soft_timeout:.0f}s")
                if frame is None:
                    break
                if isinstance(frame, Exception):
                    raise frame
                if frame.response_kind == "e2a.complete":
                    final = frame.body.get("result", {}).get("content", "") or ""
                elif frame.response_kind == "e2a.error":
                    raise RuntimeError(frame.body.get("error", "child agent error"))
                # e2a.chunk / e2a.todo_update / e2a.ask -> discarded (black-box)
            if len(final) > self._config.max_result_chars:
                final = final[: self._config.max_result_chars] + "\n…[truncated]"
            return final
        finally:
            if not runner.done():
                runner.cancel()
            try:
                await asyncio.wait_for(runner, timeout=self._config.abort_timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def execute_spawn(
        self,
        task: SubagentTaskSpec,
        parent_session_id: str,
        parent_request_id: str,
    ) -> SubagentResult:
        llm = self._resolve_llm(task.model_name)
        child_sid = f"{parent_session_id}__sub_{uuid.uuid4().hex[:8]}"
        await self._store.create_session(child_sid)
        await self._store.append(
            child_sid, {"role": "system", "content": self._child_system_prompt(task)},
            request_id=parent_request_id,
        )
        child_loop = self._build_child_loop(llm)
        child_env = E2AEnvelope(
            request_id=f"{parent_request_id}__sub_{uuid.uuid4().hex[:8]}",
            session_id=child_sid,
            method="chat.send",
            params={"query": self._build_query(task)},
        )
        child_task = asyncio.create_task(self._drive_child(child_loop, child_env))
        self._active[task.task_id] = child_task
        try:
            final = await asyncio.wait_for(child_task, timeout=self._config.hard_timeout)
            return SubagentResult(
                success=True, task_id=task.task_id, role_id=task.role_id, result=final
            )
        except SoftTimeoutError as exc:
            return SubagentResult(
                success=False, task_id=task.task_id, role_id=task.role_id,
                error=f"soft timeout: {exc}")
        except asyncio.TimeoutError:
            return SubagentResult(
                success=False, task_id=task.task_id, role_id=task.role_id,
                error=f"hard timeout after {self._config.hard_timeout:.0f}s")
        except Exception as exc:
            return SubagentResult(
                success=False, task_id=task.task_id, role_id=task.role_id,
                error=f"{type(exc).__name__}: {exc}")
        finally:
            self._active.pop(task.task_id, None)

    async def abort_active_subagents(self, reason: str = "") -> int:
        """Cancel every tracked child task (bounded by abort_timeout). Returns the
        count cancelled. Called on parent run end / disconnect (future interrupt path)."""
        tasks = [t for t in self._active.values() if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=self._config.abort_timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        return len(tasks)


def create_subagent_executor(llm, store, parent_tools, config, child_hooks=None) -> SubagentExecutor:
    return SubagentExecutor(llm, store, parent_tools, config, child_hooks=child_hooks)
