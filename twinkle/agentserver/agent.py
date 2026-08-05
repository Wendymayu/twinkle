"""ReActAgent — a ReAct-pattern agent: think -> (tool -> result)* -> answer.

run() is an async generator yielding E2AResponse frames so the ws send
boundary stays in server.py (agent never touches the socket).

Twinkle is stream-only; unary has been removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    TODO_EVENTS,
    flush_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.permission_context import set_permission_channel
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY, ApprovalPendingRecord
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.e2a.models import E2AResponse
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    MEMORY_DIR,
    SKILLS_DIR,
    WORKSPACE_DIR,
)

log = logging.getLogger("twinkle.agentserver")


# ---------------------------------------------------------------------------
# AgentRequest — pure business input, no transport-layer concepts
# ---------------------------------------------------------------------------

@dataclass
class AgentRequest:
    """One agent run's business inputs. No E2A / WebSocket concepts.

    server.py is responsible for constructing this from the transport envelope.
    """

    session_id: str
    request_id: str
    query: str
    channel: str = "web"


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Build the base system prompt injected once per session at position 0.

    Includes agent identity, runtime environment, workspace overview, and
    tool usage guidance. Dynamic values (platform, date, workspace paths)
    are resolved at injection time so the prompt stays current.
    """
    os_type = sys.platform
    today_date = date.today().isoformat()
    workspace = WORKSPACE_DIR
    memory_dir = MEMORY_DIR
    skills_dir = SKILLS_DIR

    # Windows-specific mkdir warning
    mkdir_warning = ""
    if os_type.startswith("win"):
        mkdir_warning = (
            "\n⚠️ Windows `mkdir` 不支持 `-p`！创建嵌套目录请用 "
            "PowerShell `New-Item -ItemType Directory -Path \"parent/child\" -Force` "
            "或 cmd 分步 `mkdir parent && mkdir parent\\child`。"
        )

    return f"""# 身份与行为原则

对外交流时，不要主动提及内部框架名、目录名或运行细节。

- **直接进入正题** — 少说"好的""很乐意"，直接开始做事。
- **先想再做** — 接到任务后先理清思路，想清楚步骤再执行。
- **办事严谨** — 做事牢靠是获得信任的关键。
- **尽量不拒绝** — 尽量满足合理请求，仅在涉及违法、有害或超出能力时才拒绝并说明原因。
- **简洁输出** — 不要重复表达相同的意思，每个想法只说一次。

# 运行环境

当前平台：`{os_type}`
当前日期：`{today_date}`

**必须严格使用与当前平台匹配的命令语法**，切勿混用其他平台命令。常见差异：

| 操作 | Windows | Linux/macOS |
|------|---------|-------------|
| 创建目录 | `mkdir folder` | `mkdir -p folder` |
| 查看文件 | `type file.txt` | `cat file.txt` |
| 列出文件 | `dir` | `ls -la` |
| 删除目录 | `rmdir folder` | `rm -rf folder` |{mkdir_warning}

# 工作区

以下目录仅供执行任务时内部参考，不要主动向用户展示内部路径。

| 路径 | 用途 |
|------|------|
| `{workspace}` | 工作区根目录，文件操作默认收敛于此 |
| `{memory_dir}` | 长期记忆存储 |
| `{skills_dir}` | 技能库 |

# 工具使用指南

## Todo（任务规划）

你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed", result=...) 标记完成，调 todo_list 查看进度，调 todo_get 查看单任务详情。
- 简单单步请求：直接回答或调工具，不要使用 todo。

## 长期记忆

你有跨会话记忆工具（memory_search/write_memory/read_memory/edit_memory）。何时搜索/写入的详细规则见系统注入的"长期记忆"段。

## 技能

你有技能工具（list_skill/read_skill）。可用技能清单见系统注入的"可用技能"段。"""


_MAX_HOOK_RETRIES = 3


# ---------------------------------------------------------------------------
# ReActAgent
# ---------------------------------------------------------------------------

class ReActAgent:
    """A ReAct-pattern agent: LLM think → tool calls → results → re-decide.

    Hooks are injected at construction time via *hooks*.  ``run()`` is the
    single public entry point — it processes one user message through the
    ReAct loop and yields E2AResponse frames.
    """

    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        *,
        hooks: tuple[AgentHook, ...] = (),
        max_steps: int | None = None,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        for h in hooks:
            self._hook_manager.register_hook(h)
        self._max_steps = max_steps if max_steps is not None else MAX_STEPS

    @property
    def session_store(self) -> SessionStore:
        """The SessionStore this agent reads/writes conversation history from."""
        return self._session_store

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook (kept for test injection — prefer constructor)."""
        self._hook_manager.register_hook(hook_instance)

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook."""
        self._hook_manager.unregister_hook(hook_instance)

    # -- Public entry point -------------------------------------------------

    async def run(self, request: AgentRequest) -> AsyncIterator[E2AResponse]:
        """Process one user message through the ReAct loop.

        Triggers BEFORE_INVOKE / AFTER_INVOKE hooks, delegates to the
        internal ReAct loop, writes interrupt snapshots on failure.
        """
        session_id = request.session_id
        request_id = request.request_id

        ctx = HookContext(
            agent=self,
            event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query=request.query),
            session_id=session_id,
            request_id=request_id,
            extra={},
        )

        await self._hook_manager.execute(HookEvent.BEFORE_INVOKE, ctx)

        completed_normally = False
        try:
            async for frame in self._run_react_loop(ctx, request):
                yield frame
            completed_normally = True
        except HookInterrupt:
            completed_normally = True
            yield E2AResponse(
                request_id=request_id,
                sequence=0,
                is_final=True,
                status="failed",
                response_kind="e2a.error",
                body={"error": "execution interrupted"},
            )
        except Exception as exc:
            ctx.exception = exc
            await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            if not completed_normally:
                try:
                    snapshot = await self._build_interrupt_snapshot(ctx, session_id)
                    await self._session_store.append(
                        session_id,
                        {"role": "assistant", "content": snapshot},
                        request_id=request_id,
                    )
                except asyncio.CancelledError:
                    log.warning("interrupt snapshot cancelled for session %s", session_id)
                except Exception:
                    log.exception("failed to write interrupt snapshot for session %s", session_id)
            APPROVAL_REGISTRY.clear_all_pending(session_id)
            await self._hook_manager.execute(HookEvent.AFTER_INVOKE, ctx)

    # -- ReAct loop ---------------------------------------------------------

    async def _run_react_loop(
        self,
        ctx: HookContext,
        request: AgentRequest,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop with hook trigger points.

        Model calls use manual self._hook_manager.execute() (async generator
        incompatible with @hook).  Tool calls use @hook-decorated _tool_call.
        """
        session_id = request.session_id
        request_id = request.request_id

        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
        set_permission_channel(request.channel)
        await self._fill_missing_tool_results(session_id, request_id)

        # Insert the base system prompt once per session
        messages = self._session_store.get_messages(session_id)
        if not messages or messages[0].get("role") != "system":
            await self._session_store.append(
                session_id,
                {"role": "system", "content": build_system_prompt()},
                request_id=request_id,
            )

        await self._session_store.append(
            session_id,
            {"role": "user", "content": request.query},
            request_id=request_id,
        )

        seq = 0
        full_text = ""
        for _step in range(self._max_steps):
            msgs = self._session_store.get_messages(session_id)

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())
            await self._hook_manager.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

            # -- Merge leading system messages into one -- #
            ctx.inputs.messages = self._merge_system_messages(ctx.inputs.messages)

            # Check force_finish
            ff = ctx.consume_force_finish_request()
            if ff is not None:
                yield E2AResponse(
                    request_id=request_id,
                    sequence=seq,
                    is_final=True,
                    status="succeeded",
                    response_kind="e2a.complete",
                    body={"result": {"content": str(ff.result or "")}},
                )
                return

            # -- LLM stream with retry loop -- #
            _reask = False
            for retry_attempt in range(_MAX_HOOK_RETRIES + 1):
                ctx.retry_attempt = retry_attempt
                ctx.exception = None
                try:
                    async for ev in self._llm.stream(messages=ctx.inputs.messages, tools=ctx.inputs.tools):
                        if isinstance(ev, TextDelta):
                            full_text += ev.content
                            yield E2AResponse(
                                request_id=request_id,
                                sequence=seq,
                                is_final=False,
                                status="in_progress",
                                response_kind="e2a.chunk",
                                body={"result": {"content": ev.content}},
                            )
                            seq += 1
                        elif isinstance(ev, Finish):
                            await self._session_store.append(
                                session_id,
                                ev.assistant_message,
                                request_id=request_id,
                                event_type="chat.final",
                            )
                            tcs = ev.assistant_message.get("tool_calls")
                            if ev.finish_reason == "tool_calls" and tcs:
                                if len(tcs) > 1:
                                    # --- Parallel path ---
                                    try:
                                        par_results, par_todos = await self._try_parallel_tool_calls(
                                            tcs, session_id, request_id,
                                        )
                                        for snap in par_todos:
                                            yield E2AResponse(
                                                request_id=request_id,
                                                sequence=seq, is_final=False,
                                                status="in_progress",
                                                response_kind="e2a.todo_update",
                                                body=snap,
                                            )
                                            seq += 1
                                        for tc_id, result in par_results:
                                            await self._session_store.append(
                                                session_id,
                                                {"role": "tool", "tool_call_id": tc_id, "content": result},
                                                request_id=request_id,
                                                event_type="chat.tool_result",
                                            )
                                        await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                        _reask = True
                                        break
                                    except HookInterrupt:
                                        log.info("parallel tool calls fell back to sequential (HookInterrupt)")
                                if len(tcs) <= 1 or _reask is False:
                                    # --- Sequential path ---
                                    for tc in tcs:
                                        name = tc["function"]["name"]
                                        try:
                                            args = json.loads(tc["function"]["arguments"] or "{}")
                                        except Exception:
                                            args = {}
                                        ctx.inputs = ToolCallInputs(
                                            name=name, args=args, tool_call_id=tc["id"]
                                        )
                                        try:
                                            result = await self._tool_call(ctx)
                                        except HookInterrupt as hi:
                                            if "approval_id" not in hi.data:
                                                yield E2AResponse(
                                                    request_id=request_id, sequence=seq, is_final=True,
                                                    status="failed", response_kind="e2a.error",
                                                    body={"error": "tool execution interrupted"})
                                                return
                                            # ASK: register Future + yield e2a.ask + suspend
                                            approval_id = hi.data["approval_id"]
                                            future = APPROVAL_REGISTRY.register(approval_id)
                                            APPROVAL_REGISTRY.save_pending(session_id, ApprovalPendingRecord(
                                                approval_id=approval_id,
                                                tool=hi.data["tool"],
                                                args=hi.data["args"],
                                                tool_call_id=tc["id"],
                                                reason=hi.data["reason"],
                                                request_id=request_id,
                                                session_id=session_id,
                                                created_at=time.time(),
                                            ))
                                            yield E2AResponse(
                                                request_id=request_id, sequence=seq, is_final=False,
                                                status="in_progress", response_kind="e2a.ask",
                                                body={"approval_id": approval_id, "tool": hi.data["tool"],
                                                      "args": hi.data["args"], "tool_call_id": tc["id"],
                                                      "reason": hi.data["reason"]})
                                            seq += 1
                                            decision = await future  # SUSPEND
                                            if decision in ("allow", "allow_always"):
                                                ctx.extra["_approval_decision"] = decision
                                                ctx.extra.setdefault("_approved_tool_call_ids", set()).add(tc["id"])
                                                try:
                                                    result = await self._tool_call(ctx)
                                                except HookInterrupt:
                                                    raise
                                                except Exception as exc:
                                                    result = f"[tool error] {type(exc).__name__}: {exc}"
                                            else:
                                                result = (f"[tool denied by user: {hi.data['tool']}] "
                                                          f"{hi.data.get('reason', '')}")
                                        except Exception as exc:
                                            result = f"[tool error] {type(exc).__name__}: {exc}"
                                        for snap in flush_todo_events():
                                            yield E2AResponse(
                                                request_id=request_id,
                                                sequence=seq,
                                                is_final=False,
                                                status="in_progress",
                                                response_kind="e2a.todo_update",
                                                body=snap,
                                            )
                                            seq += 1
                                        await self._session_store.append(
                                            session_id,
                                            {"role": "tool", "tool_call_id": tc["id"], "content": result},
                                            request_id=request_id,
                                            event_type="chat.tool_result",
                                        )
                                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                _reask = True
                                break
                            # Final answer
                            yield E2AResponse(
                                request_id=request_id,
                                sequence=seq,
                                is_final=True,
                                status="succeeded",
                                response_kind="e2a.complete",
                                body={"result": {"content": full_text}},
                            )
                            await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    if _reask:
                        break
                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break
                except asyncio.CancelledError:
                    raise
                except HookInterrupt:
                    raise
                except Exception as exc:
                    ctx.exception = exc
                    await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    ff = ctx.consume_force_finish_request()
                    if ff is not None:
                        yield E2AResponse(
                            request_id=request_id,
                            sequence=seq,
                            is_final=True,
                            status="succeeded",
                            response_kind="e2a.complete",
                            body={"result": {"content": str(ff.result or "")}},
                        )
                        return
                    retry_req = ctx.consume_retry_request()
                    if retry_req is not None and retry_attempt < _MAX_HOOK_RETRIES:
                        if retry_req.delay > 0:
                            await asyncio.sleep(retry_req.delay)
                        log.info("hook requested LLM retry, attempt %d/%d",
                                 retry_attempt + 1, _MAX_HOOK_RETRIES)
                        continue
                    raise
            if _reask:
                continue

        # exceeded max_steps
        yield E2AResponse(
            request_id=request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={self._max_steps}"},
        )

    # -- Parallel tool execution --------------------------------------------

    async def _try_parallel_tool_calls(
        self,
        tcs: list[dict],
        session_id: str,
        request_id: str,
    ) -> tuple[list[tuple[str, str]], list[dict]]:
        """Execute multiple tool calls concurrently via asyncio.gather.

        Each tool call gets its own HookContext (isolated inputs + extra) and
        its own TODO_EVENTS buffer so concurrent calls don't race on shared
        state.  Results are returned in the same order as *tcs*.
        """
        per_tc_results: list[tuple[str, str] | None] = [None] * len(tcs)
        per_tc_todos: list[list[dict]] = [[] for _ in range(len(tcs))]

        async def _run_one(idx: int, tc: dict) -> None:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}

            tc_todo_buffer: list[dict] = []
            TODO_EVENTS.set(tc_todo_buffer)

            tc_ctx = HookContext(
                agent=self,
                event=HookEvent.BEFORE_TOOL_CALL,
                inputs=ToolCallInputs(name=name, args=args, tool_call_id=tc["id"]),
                session_id=session_id,
                request_id=request_id,
                extra={},
            )
            try:
                result = await self._tool_call(tc_ctx)
            except HookInterrupt:
                raise
            except Exception as exc:
                result = f"[tool error] {type(exc).__name__}: {exc}"

            per_tc_results[idx] = (tc["id"], result)
            per_tc_todos[idx] = list(tc_todo_buffer)

        raw_results = await asyncio.gather(
            *[asyncio.create_task(_run_one(i, tc)) for i, tc in enumerate(tcs)],
            return_exceptions=True,
        )

        for r in raw_results:
            if isinstance(r, HookInterrupt):
                raise r

        results: list[tuple[str, str]] = []
        for r in per_tc_results:
            if r is not None:
                results.append(r)
        all_todos = [snap for tc_todos in per_tc_todos for snap in tc_todos]
        return results, all_todos

    # -- Interrupt snapshot -------------------------------------------------

    async def _build_interrupt_snapshot(self, ctx: HookContext, session_id: str) -> str:
        """Build an interrupt snapshot message from session history + TodoStore."""
        parts = ["[SYSTEM] 任务中断。"]

        if ctx.exception:
            exc_type = type(ctx.exception).__name__
            exc_msg = str(ctx.exception)
            if len(exc_msg) > 100:
                exc_msg = exc_msg[:100] + "..."
            parts.append(f"中断原因：{exc_type}: {exc_msg}")
        else:
            parts.append("中断原因：请求被取消")

        msgs = self._session_store.get_messages(session_id)
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                tools = [tc.get("function", {}).get("name", "unknown") for tc in m.get("tool_calls", [])]
                parts.append(f"中断前正在执行：{', '.join(tools)}")
                break

        try:
            from twinkle.agentserver.todo import get_todo_store
            todos = get_todo_store()
            tasks = await todos.list(session_id)
            if tasks:
                done = sum(1 for t in tasks if t.status == "completed")
                parts.append(f"任务进度：{done}/{len(tasks)} 已完成")
        except Exception:
            pass

        return " ".join(parts)

    # -- Orphan tool-result fill --------------------------------------------

    async def _fill_missing_tool_results(self, session_id: str, request_id: str) -> None:
        """Inject synthetic tool_result for orphan tool_calls from a crash."""
        msgs = self._session_store.get_messages(session_id)
        if not msgs:
            return
        last_assistant = None
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_assistant = m
                break
        if last_assistant is None:
            return

        pending = APPROVAL_REGISTRY.get_pending(session_id)
        pending_map = {p["tool_call_id"]: p for p in pending if p.get("tool_call_id")}

        for tc in last_assistant["tool_calls"]:
            tc_id = tc.get("id")
            if tc_id and not any(m.get("role") == "tool" and m.get("tool_call_id") == tc_id
                                for m in msgs):
                tool_name = tc.get("function", {}).get("name", "unknown")
                tool_args = tc.get("function", {}).get("arguments", "")
                parts = [f"[interrupted: {tool_name} was interrupted, result unknown."]
                if tool_args:
                    args_preview = tool_args[:200] + ("..." if len(tool_args) > 200 else "")
                    parts.append(f"Args: {args_preview}.")
                approval = pending_map.get(tc_id)
                if approval:
                    parts.append(f"Approval was pending (reason: {approval.get('reason', 'unknown')}).")
                parts.append("]")
                content = " ".join(parts)
                await self._session_store.append(
                    session_id,
                    {"role": "tool", "tool_call_id": tc_id, "content": content},
                    request_id=request_id)

    # -- System message merge -----------------------------------------------

    @staticmethod
    def _merge_system_messages(messages: list[dict]) -> list[dict]:
        """Merge all leading system-role messages into ONE.

        Order: identity → skill → memory → summary → other.
        """
        if not messages:
            return messages

        system_msgs: list[dict] = []
        rest_start = 0
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                system_msgs.append(m)
                rest_start = i + 1
            else:
                break

        if len(system_msgs) <= 1:
            return messages

        rest = messages[rest_start:]

        identity_parts: list[str] = []
        skill_parts: list[str] = []
        memory_parts: list[str] = []
        summary_parts: list[str] = []
        other_parts: list[str] = []

        _IDENTITY_PREFIX = "# 身份与行为原则"
        _SKILL_PREFIXES = ("## 可用技能", "你有 skills")
        _MEMORY_PREFIX = "## 长期记忆"
        _SUMMARY_PREFIX = "[prior context summary]"

        for m in system_msgs:
            content = m.get("content", "")
            if content.startswith(_IDENTITY_PREFIX):
                identity_parts.append(content)
            elif any(content.startswith(p) for p in _SKILL_PREFIXES):
                skill_parts.append(content)
            elif content.startswith(_MEMORY_PREFIX):
                memory_parts.append(content)
            elif content.startswith(_SUMMARY_PREFIX):
                summary_parts.append(content)
            else:
                other_parts.append(content)

        merged_content = "\n\n".join(
            part for part in (
                identity_parts + skill_parts + memory_parts
                + summary_parts + other_parts
            ) if part
        )

        return [{"role": "system", "content": merged_content}] + rest

    # -- @hook-decorated tool call ------------------------------------------

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _tool_call(self, ctx: HookContext) -> str:
        """Tool execution wrapped with @hook lifecycle."""
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        return await self._tool_manager.execute(inputs.name, inputs.args)
