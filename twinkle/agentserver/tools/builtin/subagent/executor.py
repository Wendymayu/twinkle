"""SubagentExecutor — builds + runs an isolated child AgentLoop (black-box).

execute_subagent: fresh child session, trimmed ToolManager (no spawn_subagent /
memory-writes), reused LLMClient/SessionStore (the child always uses the
parent's llm — no per-subagent model override), tighter max_steps; runs child
run_stream in a child asyncio task (ContextVar isolation) with soft/hard
timeouts; returns the child's e2a.complete content as a SubagentResult.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.builtin import LoggingHook, MemoryHook, SkillHook
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.builtin.subagent.models import (
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

# 子 agent 角色

你是被父 agent 委派的隔离子 agent，执行一个聚焦子任务。你的输出会回灌给父 agent（作 tool_result），不直接面向用户。

- 你看不到父的对话历史；所需一切在 user 消息（objective）里。缺东西就尽力而为，不要反问用户（你无直连通道）。
- 记忆只读：用 memory_search/read_memory 检索；不要写或改长期记忆。
- 可按需用 skill：list_skill 看清单，read_skill 载入指令。
- 用 ReAct 完成子任务；把最终答案作为最终消息返回。若无法完成，返回简短失败说明（别卡住、别空转）。
- 聚焦、简洁。
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
        self._llm = llm                    # child reuses the parent's LLMClient
        self._store = store
        self._parent_tools = parent_tools
        self._config = config
        self._child_hooks = child_hooks    # None -> default fresh list per child

    def _build_tool_manager(self) -> ToolManager:
        tool_manager = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name not in EXCLUDED_TOOLS:
                tool_manager.register(t)
        return tool_manager

    def _system_prompt(self) -> str:
        # Reuse the parent's base prompt (identity + runtime + workspace + tool
        # guidance) so the child uses command_exec/file tools correctly, then
        # append the sub-agent role addendum. Pre-seeding this as the first
        # message also makes _inner_run_stream skip its default build_system_prompt() seed.
        from twinkle.agentserver.agent_loop import build_system_prompt  # lazy: avoid circular (agent_loop -> tools -> subagent -> executor)
        return build_system_prompt() + "\n\n" + _SUBAGENT_ADDENDUM

    def _build_query(self, task: SubagentTaskSpec) -> str:
        if task.prompt:
            return f"{task.objective}\n\n{task.prompt}"
        return task.objective

    def _hook_list(self) -> list["AgentHook"]:
        if self._child_hooks is not None:
            return self._child_hooks
        return [SkillHook(), MemoryHook(), LoggingHook()]

    # --- build + run ---

    def _build_loop(self) -> AgentLoop:
        from twinkle.agentserver.agent_loop import AgentLoop  # lazy: avoid circular (agent_loop -> tools -> subagent -> executor)
        tool_manager = self._build_tool_manager()
        loop = AgentLoop(self._llm, self._store, tool_manager,  # type: ignore[arg-type]
                               max_steps=self._config.max_steps)
        for hook in self._hook_list():
            loop.register_hook(hook)
        return loop

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

    async def execute_subagent(
        self,
        task: SubagentTaskSpec,
        parent_session_id: str,
        parent_request_id: str,
    ) -> SubagentResult:
        session_id = f"{parent_session_id}__sub_{uuid.uuid4().hex[:8]}"
        await self._store.create_session(session_id)
        await self._store.append(
            session_id, {"role": "system", "content": self._system_prompt()},
            request_id=parent_request_id,
        )
        loop = self._build_loop()
        envelope = E2AEnvelope(
            request_id=f"{parent_request_id}__sub_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            method="chat.send",
            params={"query": self._build_query(task)},
        )
        child_task = asyncio.create_task(self._drive_child(loop, envelope))
        try:
            final = await asyncio.wait_for(child_task, timeout=self._config.hard_timeout)
            return SubagentResult(success=True, result=final)
        except SoftTimeoutError as exc:
            return SubagentResult(success=False, error=f"soft timeout: {exc}")
        except asyncio.TimeoutError:
            return SubagentResult(success=False, error=f"hard timeout after {self._config.hard_timeout:.0f}s")
        except Exception as exc:
            return SubagentResult(success=False, error=f"{type(exc).__name__}: {exc}")


def create_subagent_executor(llm, store, parent_tools, config, child_hooks=None) -> SubagentExecutor:
    return SubagentExecutor(llm, store, parent_tools, config, child_hooks=child_hooks)
