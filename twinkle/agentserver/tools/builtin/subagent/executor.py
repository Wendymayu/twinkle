"""SubagentExecutor —— 构建 + 运行一个隔离子 ReActAgent(黑盒)。

execute_subagent:全新子 session,裁剪过的 ToolManager(无 spawn_subagent /
memory 写入),复用 LLMClient/SessionStore(子始终用父的 llm —— 无
per-subagent 模型覆盖),无 step cap(忙跑由 RepeatToolCallDetector
CRITICAL force_finish + hard_timeout 兜底);在子 asyncio task 中
运行子的 run_stream(ContextVar 隔离),带 soft/hard 超时;把子的
e2a.complete content 作为 SubagentResult 返回。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from twinkle.agentserver.hooks.builtin import (
    AuditHook, LoggingHook, MemoryFlushHook, MemoryHook, RepeatToolCallDetectorHook,
    RetryHook, RuntimeEnvHook, SkillHook)
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.tools.builtin.subagent.models import (
    EXCLUDED_TOOLS,
    SoftTimeoutError,
    SubagentResult,
    SubagentTaskSpec,
)
# AgentRequest 在方法内延迟导入以避免循环 import:
#   agent -> ToolManager -> subagent -> executor -> agent

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
        self._llm = llm                    # 子复用父的 LLMClient
        self._store = store
        self._parent_tools = parent_tools
        self._config = config
        self._child_hooks = child_hooks    # None -> 每个子用全新默认列表

    def _build_tool_manager(self) -> ToolManager:
        tool_manager = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name not in EXCLUDED_TOOLS:
                tool_manager.register(t)
        return tool_manager

    def _build_query(self, task: SubagentTaskSpec) -> str:
        if task.prompt:
            return f"{task.objective}\n\n{task.prompt}"
        return task.objective

    def _hook_list(self) -> list["AgentHook"]:
        if self._child_hooks is not None:
            return self._child_hooks
        return [SkillHook(), MemoryHook(), MemoryFlushHook(llm=self._llm),
                LoggingHook(), RepeatToolCallDetectorHook(), RetryHook(), RuntimeEnvHook(),
                AuditHook()]

    # --- 构建 + 运行 ---

    def _build_child_agent(self) -> "ReActAgent":
        from twinkle.agentserver.agent import ReActAgent, normal_base_sections  # 延迟:避免循环
        tool_manager = self._build_tool_manager()
        # 子身份 = 父的 base prompt(priority 10)+ 子 agent 补充段(priority 15)
        base_sections = normal_base_sections() + [
            PromptSection("subagent_addendum", _SUBAGENT_ADDENDUM, priority=15)]
        return ReActAgent(self._llm, self._store, tool_manager,
                          hooks=tuple(self._hook_list()),
                          base_sections=base_sections)

    async def _drive_child(self, child_loop: "ReActAgent", child_request: "AgentRequest") -> str:
        """在子 task 中运行子 agent(ContextVar 隔离);经 queue
        汇集 frame;返回 e2a.complete content。黑盒:chunk /
        todo_update frame 被丢弃。"""
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            try:
                async for frame in child_loop.run(child_request):
                    await queue.put(frame)
            except Exception as exc:       # 子抛异常 -> 作为 frame 转发
                await queue.put(exc)
            finally:
                await queue.put(None)       # 哨兵

        runner = asyncio.create_task(_run())   # context 副本 -> 子的 ContextVar.set 不外泄
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
                # e2a.chunk / e2a.todo_update / e2a.ask -> 丢弃(黑盒)
            if len(final) > self._config.max_result_chars:
                final = final[: self._config.max_result_chars] + "\n…[truncated]"
            return final
        finally:
            if not runner.done():
                runner.cancel()
            try:
                await asyncio.wait_for(runner, timeout=self._config.abort_timeout)
            except asyncio.CancelledError:
                # 预期:取消 runner 会把 CancelledError 传回这里。
                # 任何外部取消(如 hard_timeout)在此 finally 后仍会继续。
                pass
            except asyncio.TimeoutError:
                # 协作但缓慢的清理超出窗口;runner 可能卡在不可取消的
                # 代码里(孤儿风险)。注意:吞掉 CancelledError 的 runner 会让
                # wait_for 完全挂起 —— abort_timeout 并不约束这种情况;
                # 真正的保证在于子的 await 是可取消的。
                log.warning(
                    "subagent reap: runner did not finish cancellation within %.0fs "
                    "(orphan risk: stuck in non-cancellable code?)",
                    self._config.abort_timeout)
            except Exception as exc:
                # 防御性:_run 把所有 Exception 转成 frame 转发,故此分支极少
                # 命中;记日志而非静默吞掉 reap 路径里的真实 bug。
                log.warning("subagent reap: runner raised unexpected error: %r", exc)

    async def execute_subagent(
        self,
        task: SubagentTaskSpec,
        parent_session_id: str,
        parent_request_id: str,
    ) -> SubagentResult:
        session_id = f"{parent_session_id}__sub_{uuid.uuid4().hex[:8]}"
        await self._store.create_session(session_id)
        child_agent = self._build_child_agent()
        from twinkle.agentserver.agent import AgentRequest  # 延迟:避免循环
        child_request = AgentRequest(
            session_id=session_id,
            request_id=f"{parent_request_id}__sub_{uuid.uuid4().hex[:8]}",
            query=self._build_query(task),
        )
        child_task = asyncio.create_task(self._drive_child(child_agent, child_request))
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
