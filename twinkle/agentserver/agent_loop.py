"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).

Twinkle is stream-only; run_unary has been removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.todo import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.permission_context import set_permission_channel
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY
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
from twinkle.agentserver.context_compression import compress_messages
from twinkle.config import (
    AGENT_MAX_STEPS as MAX_STEPS,
    CONTEXT_KEEP_RECENT_PAIRS,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_TOKEN_THRESHOLD,
)

log = logging.getLogger("twinkle.agentserver")

TODO_SYSTEM_PROMPT = (
    "You have todo tools to plan and track multi-step work: "
    "todo_create, todo_complete, todo_list. For non-trivial multi-step "
    "requests, first call todo_create with a list of sub-tasks, then work "
    "through them calling todo_complete(idx, result) as each finishes, and "
    "call todo_list to check progress. For simple one-step requests, do NOT "
    "use the todo tools — just answer or call the needed tool directly."
)

_MAX_HOOK_RETRIES = 3


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        memory: LongTermMemory,
        permission=None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._tools = tools
        self._memory = memory
        self._permission = permission
        self._hooks = HookManager(self)

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook on this loop (sync — safe to call from build_agent_loop)."""
        self._hooks.register_hook(hook_instance)

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook from this loop."""
        self._hooks.unregister_hook(hook_instance)

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

        await self._hooks.execute(HookEvent.BEFORE_INVOKE, ctx)

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
            await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            await self._hooks.execute(HookEvent.AFTER_INVOKE, ctx)

    # --- ReAct core with hook trigger points --- #

    async def _inner_run_stream(
        self,
        ctx: HookContext,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop with hook trigger points + context compression inserted.

        Model calls use manual self._hooks.execute() (async generator incompatible with @hook).
        Tool calls use @hook-decorated _raided_tool_call.
        Context compression runs before each LLM call.
        """
        session_id = envelope.session_id
        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
        set_permission_channel(envelope.channel or "web")
        await self._sanitize_orphan_tool_calls(session_id, envelope.request_id)
        # Insert the todo-guidance system message once per session
        existing = self._store.get_messages(session_id)
        if not existing or existing[0].get("role") != "system":
            await self._store.append(
                session_id,
                {"role": "system", "content": TODO_SYSTEM_PROMPT},
                request_id=envelope.request_id,
            )
        query = (envelope.params or {}).get("query", "")
        await self._store.append(
            session_id,
            {"role": "user", "content": query},
            request_id=envelope.request_id,
        )
        self._memory.recall(query)

        seq = 0
        full_text = ""
        for _step in range(MAX_STEPS):
            msgs = self._store.get_messages(session_id)

            # -- Context compression (before hook trigger) -- #
            msgs = await compress_messages(
                msgs,
                self._llm,
                token_threshold=CONTEXT_TOKEN_THRESHOLD,
                keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS,
                summary_system_prompt=CONTEXT_SUMMARY_PROMPT,
            )

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tools.schemas())
            await self._hooks.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

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
                            await self._store.append(
                                session_id,
                                ev.assistant_message,
                                request_id=envelope.request_id,
                                event_type="chat.final",
                            )
                            tcs = ev.assistant_message.get("tool_calls")
                            if ev.finish_reason == "tool_calls" and tcs:
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
                                        result = await self._raided_tool_call(ctx, name, args)
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
                                        yield E2AResponse(
                                            request_id=envelope.request_id, sequence=seq, is_final=False,
                                            status="in_progress", response_kind="e2a.ask",
                                            body={"approval_id": approval_id, "tool": hi.data["tool"],
                                                  "args": hi.data["args"], "tool_call_id": tc["id"],
                                                  "reason": hi.data["reason"]})
                                        seq += 1
                                        decision = await future  # SUSPEND — ws_handler concurrency resumes it
                                        if decision in ("allow", "allow_always"):
                                            if decision == "allow_always" and self._permission is not None:
                                                await self._permission.persist_allow_always(hi.data)
                                            ctx.extra.setdefault("_approved_tool_call_ids", set()).add(tc["id"])
                                            result = await self._raided_tool_call(ctx, name, args)
                                        else:
                                            result = (f"[tool denied by user: {hi.data['tool']}] "
                                                      f"{hi.data.get('reason', '')}")
                                    for snap in drain_todo_events():
                                        yield E2AResponse(
                                            request_id=envelope.request_id,
                                            sequence=seq,
                                            is_final=False,
                                            status="in_progress",
                                            response_kind="e2a.todo_update",
                                            body=snap,
                                        )
                                        seq += 1
                                    await self._store.append(
                                        session_id,
                                        {
                                            "role": "tool",
                                            "tool_call_id": tc["id"],
                                            "content": result,
                                        },
                                        request_id=envelope.request_id,
                                        event_type="chat.tool_result",
                                    )
                                # AFTER_MODEL_CALL for tool_calls turn
                                await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
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
                            await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    if _reask:
                        break  # exit retry loop; outer _step loop will continue
                    # LLM stream ended without Finish — shouldn't happen, but handle gracefully
                    await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break  # exit retry loop, fall through to next step
                except asyncio.CancelledError:
                    raise  # never interfere with cancellation
                except HookInterrupt:
                    raise  # interrupt propagates immediately
                except Exception as exc:
                    ctx.exception = exc
                    await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    retry_req = ctx.consume_retry_request()
                    if retry_req is not None and retry_attempt < _MAX_HOOK_RETRIES:
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
            body={"error": f"agent loop exceeded max_steps={MAX_STEPS}"},
        )

    async def _sanitize_orphan_tool_calls(self, session_id: str, request_id: str) -> None:
        """If the session's most recent assistant-with-tool_calls message lacks
        results for some of its tool_calls (a crash mid-approval, possibly after
        some results were already appended), inject a synthetic tool_result for
        each missing tool_call_id so the next LLM call doesn't error on orphan
        tool_calls."""
        msgs = self._store.get_messages(session_id)
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
                await self._store.append(
                    session_id,
                    {"role": "tool", "tool_call_id": tc_id,
                     "content": "[interrupted: previous request did not complete]"},
                    request_id=request_id)

    # --- @hook-decorated methods --- #

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _raided_tool_call(
        self,
        ctx: HookContext,
        name: str,
        args: dict,
    ) -> str:
        """Tool execution wrapped with @hook lifecycle."""
        return await self._tools.execute(name, args)
