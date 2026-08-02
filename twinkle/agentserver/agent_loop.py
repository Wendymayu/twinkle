"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).

Twinkle is stream-only; run_unary has been removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import sys
import time
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
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    MEMORY_DIR,
    SKILLS_DIR,
    WORKSPACE_DIR,
)

log = logging.getLogger("twinkle.agentserver")


def build_system_prompt() -> str:
    """Build the base system prompt injected once per session at position 0.

    Includes agent identity, runtime environment, workspace overview, and
    tool usage guidance. Dynamic values (platform, date, workspace paths)
    are resolved at injection time so the prompt stays current.
    """
    os_type = sys.platform
    today = date.today().isoformat()
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
当前日期：`{today}`

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


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        max_steps: int | None = None,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
        self._max_steps = max_steps if max_steps is not None else MAX_STEPS

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook on this loop (sync — safe to call from build_agent_loop)."""
        self._hook_manager.register_hook(hook_instance)

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook from this loop."""
        self._hook_manager.unregister_hook(hook_instance)

    # --- Public entry point — signature unchanged --- #

    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        """Entry point — creates HookContext, triggers BEFORE/AFTER_INVOKE,
        delegates ReAct logic to _inner_run_stream.

        Signature unchanged: (envelope) -> AsyncIterator[E2AResponse].
        """
        session_id = envelope.session_id
        request_id = envelope.request_id
        query = (envelope.params or {}).get("query", "")

        ctx = HookContext(
            agent=self,
            event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query=query, envelope=envelope),
            session_id=session_id,
            request_id=request_id,
            extra={},
        )

        await self._hook_manager.execute(HookEvent.BEFORE_INVOKE, ctx)

        try:
            async for frame in self._inner_run_stream(ctx, envelope):
                yield frame
        except HookInterrupt:
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
            # Clear any lingering pending approvals for this session (safety net)
            APPROVAL_REGISTRY.clear_all_pending(session_id)
            await self._hook_manager.execute(HookEvent.AFTER_INVOKE, ctx)

    # --- ReAct core with hook trigger points --- #

    async def _inner_run_stream(
        self,
        ctx: HookContext,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop with hook trigger points.

        Model calls use manual self._hook_manager.execute() (async generator incompatible with @hook).
        Tool calls use @hook-decorated _hooked_tool_call.
        """
        session_id = envelope.session_id
        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
        set_permission_channel(envelope.channel or "web")
        await self._sanitize_orphan_tool_calls(session_id, envelope.request_id)
        # Insert the base system prompt once per session
        messages = self._session_store.get_messages(session_id)
        if not messages or messages[0].get("role") != "system":
            await self._session_store.append(
                session_id,
                {"role": "system", "content": build_system_prompt()},
                request_id=envelope.request_id,
            )
        query = (envelope.params or {}).get("query", "")
        await self._session_store.append(
            session_id,
            {"role": "user", "content": query},
            request_id=envelope.request_id,
        )
        seq = 0
        full_text = ""
        for _step in range(self._max_steps):
            msgs = self._session_store.get_messages(session_id)

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())
            await self._hook_manager.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

            # -- Merge leading system messages into one (identity first, operational last) -- #
            # Mirrors jiuwenswarm's SystemPromptBuilder: a single merged system prompt
            # avoids "lost in the middle" — identity gets the beginning-attention hotspot,
            # operational sections stay close to conversation for recency bias.
            ctx.inputs.messages = self._merge_system_messages(ctx.inputs.messages)

            # Check force_finish — skip LLM call if requested
            ff = ctx.consume_force_finish_request()
            if ff is not None:
                yield E2AResponse(
                    request_id=envelope.request_id,
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
                    # Use ctx.inputs.messages (not stale local msgs) so that a
                    # context-compression hook that replaces ctx.inputs.messages
                    # during ON_MODEL_EXCEPTION takes effect on retry.
                    async for ev in self._llm.stream(messages=ctx.inputs.messages, tools=ctx.inputs.tools):
                        if isinstance(ev, TextDelta):
                            full_text += ev.content
                            yield E2AResponse(
                                request_id=envelope.request_id,
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
                                request_id=envelope.request_id,
                                event_type="chat.final",
                            )
                            tcs = ev.assistant_message.get("tool_calls")
                            if ev.finish_reason == "tool_calls" and tcs:
                                if len(tcs) > 1:
                                    # --- Parallel path: concurrent execution via asyncio.gather ---
                                    try:
                                        par_results, par_todos = await self._try_parallel_tool_calls(
                                            tcs, session_id, envelope.request_id,
                                        )
                                        # Emit todo frames + append tool results in order
                                        for snap in par_todos:
                                            yield E2AResponse(
                                                request_id=envelope.request_id,
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
                                                request_id=envelope.request_id,
                                                event_type="chat.tool_result",
                                            )
                                        # AFTER_MODEL_CALL for tool_calls turn
                                        await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                        _reask = True
                                        break  # exit async-for loop
                                    except HookInterrupt:
                                        # ASK needs yield — fall through to sequential path
                                        log.info("parallel tool calls fell back to sequential (HookInterrupt)")
                                        # Fall through to sequential path below
                                if len(tcs) <= 1 or _reask is False:
                                    # --- Sequential path (single tc, or fallback from parallel) ---
                                    for tc in tcs:
                                        name = tc["function"]["name"]
                                        try:
                                            args = json.loads(tc["function"]["arguments"] or "{}")
                                        except Exception:
                                            args = {}
                                        # Tool call via @hook-decorated method
                                        ctx.inputs = ToolCallInputs(
                                            name=name, args=args, tool_call_id=tc["id"]
                                        )
                                        try:
                                            result = await self._hooked_tool_call(ctx)
                                        except HookInterrupt as hi:
                                            if "approval_id" not in hi.data:
                                                yield E2AResponse(
                                                    request_id=envelope.request_id, sequence=seq, is_final=True,
                                                    status="failed", response_kind="e2a.error",
                                                    body={"error": "tool execution interrupted"})
                                                return
                                            # ASK: register Future + yield e2a.ask + suspend await
                                            approval_id = hi.data["approval_id"]
                                            future = APPROVAL_REGISTRY.register(approval_id)
                                            # PERSIST: save approval state to disk for reconnection recovery
                                            APPROVAL_REGISTRY.save_pending(session_id, ApprovalPendingRecord(
                                                approval_id=approval_id,
                                                tool=hi.data["tool"],
                                                args=hi.data["args"],
                                                tool_call_id=tc["id"],
                                                reason=hi.data["reason"],
                                                request_id=envelope.request_id,
                                                session_id=session_id,
                                                created_at=time.time(),
                                            ))
                                            yield E2AResponse(
                                                request_id=envelope.request_id, sequence=seq, is_final=False,
                                                status="in_progress", response_kind="e2a.ask",
                                                body={"approval_id": approval_id, "tool": hi.data["tool"],
                                                      "args": hi.data["args"], "tool_call_id": tc["id"],
                                                      "reason": hi.data["reason"]})
                                            seq += 1
                                            decision = await future  # SUSPEND — ws_handler concurrency resumes it
                                            if decision in ("allow", "allow_always"):
                                                # Record approval decision in ctx.extra so
                                                # PermissionHook's bypass branch can persist
                                                # allow_always — AgentLoop doesn't hold engine.
                                                ctx.extra["_approval_decision"] = decision
                                                ctx.extra.setdefault("_approved_tool_call_ids", set()).add(tc["id"])
                                                try:
                                                    result = await self._hooked_tool_call(ctx)
                                                except HookInterrupt:
                                                    raise  # bypass already applied; a second interrupt shouldn't occur
                                                except Exception as exc:
                                                    result = f"[tool error] {type(exc).__name__}: {exc}"
                                            else:
                                                result = (f"[tool denied by user: {hi.data['tool']}] "
                                                          f"{hi.data.get('reason', '')}")
                                        except Exception as exc:
                                            # Non-HookInterrupt tool failure (after @hook retry exhausted
                                            # or non-transient): turn into a tool_result string so the loop
                                            # keeps going instead of crashing the agent loop.
                                            result = f"[tool error] {type(exc).__name__}: {exc}"
                                        for snap in flush_todo_events():
                                            yield E2AResponse(
                                                request_id=envelope.request_id,
                                                sequence=seq,
                                                is_final=False,
                                                status="in_progress",
                                                response_kind="e2a.todo_update",
                                                body=snap,
                                            )
                                            seq += 1
                                        await self._session_store.append(
                                            session_id,
                                            {
                                                "role": "tool",
                                                "tool_call_id": tc["id"],
                                                "content": result,
                                            },
                                            request_id=envelope.request_id,
                                            event_type="chat.tool_result",
                                        )
                                    # AFTER_MODEL_CALL for tool_calls turn (sequential path)
                                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                _reask = True
                                break  # exit async-for loop; retry loop will also break
                            # AFTER_MODEL_CALL for final answer turn
                            yield E2AResponse(
                                request_id=envelope.request_id,
                                sequence=seq,
                                is_final=True,
                                status="succeeded",
                                response_kind="e2a.complete",
                                body={"result": {"content": full_text}},
                            )
                            await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    if _reask:
                        break  # exit retry loop; outer _step loop will continue
                    # LLM stream ended without Finish — shouldn't happen, but handle gracefully
                    await self._hook_manager.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break  # exit retry loop, fall through to next step
                except asyncio.CancelledError:
                    raise  # never interfere with cancellation
                except HookInterrupt:
                    raise  # interrupt propagates immediately
                except Exception as exc:
                    ctx.exception = exc
                    await self._hook_manager.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    # Check force_finish first — e.g., overflow circuit breaker
                    ff = ctx.consume_force_finish_request()
                    if ff is not None:
                        yield E2AResponse(
                            request_id=envelope.request_id,
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
                        continue  # retry the LLM call
                    raise  # no retry or max attempts exceeded
            if _reask:
                continue  # next _step: re-ask model with tool results

        # exceeded max_steps without converging
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={self._max_steps}"},
        )

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

        Returns:
            (results, todo_snaps) where results is [(tool_call_id, result_str)]
            and todo_snaps is the merged list of todo events from all calls.

        Raises:
            HookInterrupt: if any tool call triggers a permission ASK — the
            caller must fall back to sequential execution to yield the
            e2a.ask frame.
        """
        per_tc_results: list[tuple[str, str] | None] = [None] * len(tcs)
        per_tc_todos: list[list[dict]] = [[] for _ in range(len(tcs))]

        async def _run_one(idx: int, tc: dict) -> None:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}

            # Isolated TODO buffer for this tool call
            tc_todo_buffer: list[dict] = []
            TODO_EVENTS.set(tc_todo_buffer)

            tc_ctx = HookContext(
                agent=self,
                event=HookEvent.BEFORE_TOOL_CALL,
                inputs=ToolCallInputs(name=name, args=args, tool_call_id=tc["id"]),
                session_id=session_id,
                request_id=request_id,
                extra={},  # isolated — no shared approval state
            )
            try:
                result = await self._hooked_tool_call(tc_ctx)
            except HookInterrupt:
                raise  # signal caller to fall back to sequential
            except Exception as exc:
                result = f"[tool error] {type(exc).__name__}: {exc}"

            per_tc_results[idx] = (tc["id"], result)
            per_tc_todos[idx] = list(tc_todo_buffer)

        raw_results = await asyncio.gather(
            *[asyncio.create_task(_run_one(i, tc)) for i, tc in enumerate(tcs)],
            return_exceptions=True,
        )

        # Check for HookInterrupt — any means we must fall back to sequential
        for r in raw_results:
            if isinstance(r, HookInterrupt):
                raise r

        # Collect results in order — all should be None (successful _run_one returns None)
        results: list[tuple[str, str]] = []
        for r in per_tc_results:
            if r is not None:
                results.append(r)
        all_todos = [snap for tc_todos in per_tc_todos for snap in tc_todos]
        return results, all_todos

    async def _sanitize_orphan_tool_calls(self, session_id: str, request_id: str) -> None:
        """If the session's most recent assistant-with-tool_calls message lacks
        results for some of its tool_calls (a crash mid-approval, possibly after
        some results were already appended), inject a synthetic tool_result for
        each missing tool_call_id so the next LLM call doesn't error on orphan
        tool_calls."""
        msgs = self._session_store.get_messages(session_id)
        if not msgs:
            return
        # find the LAST assistant message that carries tool_calls — the only one
        # that could be orphaned by a mid-batch crash (earlier assistants are
        # complete, or the LLM would have errored before reaching this one).
        last_assistant = None
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_assistant = m
                break
        if last_assistant is None:
            return
        for tc in last_assistant["tool_calls"]:
            tc_id = tc.get("id")
            if tc_id and not any(m.get("role") == "tool" and m.get("tool_call_id") == tc_id
                                for m in msgs):
                await self._session_store.append(
                    session_id,
                    {"role": "tool", "tool_call_id": tc_id,
                     "content": "[interrupted: previous request did not complete]"},
                    request_id=request_id)

    @staticmethod
    def _merge_system_messages(messages: list[dict]) -> list[dict]:
        """Merge all leading system-role messages into ONE, ordered so that
        identity/principles appear first and operational instructions last.

        Mirrors jiuwenswarm's SystemPromptBuilder: a single merged system
        prompt avoids "lost in the middle" — identity gets the beginning
        attention hotspot, skill/memory/compression sections stay closer
        to the conversation for recency bias.

        Ordering within the merged content:
          1. SYSTEM_PROMPT (starts with "# 身份与行为原则")
          2. Skill section (starts with "## 可用技能" or "你有 skills")
          3. Memory section (starts with "## 长期记忆")
          4. Compression summary (starts with "[prior context summary]")
          5. Any other system messages (preserve original order)
        """
        if not messages:
            return messages

        # Collect consecutive system messages at the head of the list
        system_msgs: list[dict] = []
        rest_start = 0
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                system_msgs.append(m)
                rest_start = i + 1
            else:
                break

        # If only one system message or none, no merge needed
        if len(system_msgs) <= 1:
            return messages

        rest = messages[rest_start:]

        # Classify by content prefix for ordering
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

        # Assemble: identity → skill → memory → summary → other
        merged_content = "\n\n".join(
            part for part in (
                identity_parts + skill_parts + memory_parts
                + summary_parts + other_parts
            ) if part
        )

        return [{"role": "system", "content": merged_content}] + rest

    # --- @hook-decorated methods --- #

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _hooked_tool_call(self, ctx: HookContext) -> str:
        """Tool execution wrapped with @hook lifecycle.

        Reads tool name/args from ctx.inputs (ToolCallInputs) — single data
        channel so hooks that modify ctx.inputs.args (e.g. arg sanitization)
        take effect in the method body automatically.
        """
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        return await self._tool_manager.execute(inputs.name, inputs.args)
