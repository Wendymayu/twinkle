"""AgentServer — the heavy execution core process.

Phase 1: a `websockets` server that dispatches inbound E2A envelopes to an
AgentLoop (ReAct: think -> tool -> result -> re-decide). Stream-only; no
unary mode. ws_handler(loop, store) lets tests inject a fake loop;
create_agent(store) wires the real config-driven loop for production.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve

from twinkle.agentserver.agent import AgentRequest, ReActAgent
from twinkle.agentserver.hooks.base import AgentHook
from twinkle.agentserver.hooks.builtin import LoggingHook
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.sessions import (
    SessionStore, session_store, dispatch_session_rpc, handles_session_rpc,
)
from twinkle.agentserver.skills import get_skillnet_client
from twinkle.agentserver.skills.rpc import dispatch_skill_rpc, handles_skill_rpc, run_skill_rpc
from twinkle.agentserver.tools import tool_manager
from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT
from twinkle.config import EVOLUTION_ENABLED as _EVOLUTION_ENABLED

from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.schema.message import EventType

log = logging.getLogger("twinkle.agentserver")

# 在途请求数（Dreaming busy-backoff 判据）。run_task inc/dec。
_inflight_count: int = 0


def _get_inflight_count() -> int:
    """当前在途 agent 请求数（Dreaming busy-backoff 判据）。每次调用读最新值。"""
    return _inflight_count

ACK_FRAME = {
    "type": "event",
    "event": EventType.CONNECTION_ACK.value,
    "payload": {"status": "ready"},
}


async def _safe_send(ws: ServerConnection, resp: E2AResponse) -> None:
    """Send an E2AResponse; silently swallow ConnectionClosed (client gone)."""
    try:
        await ws.send(resp.model_dump_json())
    except Exception:
        # ConnectionClosedOK / ConnectionClosedError — client disconnected.
        # No point logging at ERROR; this is a normal lifecycle event.
        log.debug("send on closed connection, dropping %s", resp.request_id)


def create_agent(store: SessionStore, hooks: list[AgentHook] | None = None, llm: LLMClient | None = None) -> ReActAgent:
    """Production wiring — config-driven ReActAgent backed by *store*.

    *store* is injected so the caller controls which SessionStore instance
    the agent (chat/ReAct path) and ``ws_handler`` (RPC path) share.
    *hooks* is the list of AgentHook instances to register (callers that need
    permission enforcement pass PermissionHook; minimal test setups pass an
    empty list). *llm* is an optional override (tests inject a scripted client;
    default = config-driven LLMClient).

    Subagent is always on: build the SubagentExecutor and auto-wire
    SubagentContextHook(executor) (alongside the caller's hooks). The
    spawn_subagent tool is registered in tool_manager() like the other builtins
    (it needs no executor at registration — it reads one from the ContextVar at
    invoke). SubagentContextHook is auto-wired (not caller-passed) because its
    dependency (the executor) is built here from the agent's llm/store/tools —
    mirroring jiuwenswarm's adapter, which binds the executor onto its stream
    rail. The caller's hooks (PermissionHook etc.) have external/no deps and
    are caller-passed.

    ContextCompressionHook is likewise auto-wired (not caller-passed): its sole
    dependency (llm) is available here.
    """
    if llm is None:
        llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL, timeout=LLM_TIMEOUT)
    tools = tool_manager()
    from twinkle.agentserver.tools.builtin.subagent import create_subagent_executor
    from twinkle.agentserver.hooks.builtin import (
        SubagentContextHook, ContextCompressionHook,
        ContextOverflowRecoveryHook, MemoryFlushHook,
        RepeatToolCallDetectorHook, RuntimeEnvHook,
    )
    from twinkle.agentserver.workflow.tools import WorkflowContextHook
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config import settings
    executor = create_subagent_executor(
        llm=llm, store=store, parent_tools=tools, config=settings.subagent
    )
    workflow_executor = WorkflowExecutor(
        llm=llm, tools=tools, subagent_executor=executor,
        config=settings.workflow,
    )
    all_hooks = list(hooks or []) + [
        SubagentContextHook(executor),
        WorkflowContextHook(workflow_executor),
        ContextCompressionHook(llm=llm),
        MemoryFlushHook(llm=llm),
        ContextOverflowRecoveryHook(llm=llm),
        RepeatToolCallDetectorHook(),
        RuntimeEnvHook(),
    ]
    if _EVOLUTION_ENABLED:
        from twinkle.agentserver.evolution import get_orchestrator
        from twinkle.agentserver.hooks.builtin import SkillEvolutionHook
        all_hooks.append(SkillEvolutionHook(orchestrator=get_orchestrator()))
    # Team infrastructure — always wired; activates only when request.mode == "team"
    from twinkle.agentserver.team.manager import TeamManager
    from twinkle.agentserver.hooks.builtin import TeamContextHook
    team_mgr = TeamManager(llm=llm, store=store, parent_tools=tools, config=settings.team)
    all_hooks.append(TeamContextHook(team_mgr))
    return ReActAgent(llm, store, tools, hooks=tuple(all_hooks))


def ws_handler(agent: ReActAgent) -> Callable[[ServerConnection], Awaitable[None]]:
    """Return a ws handler bound to *agent*.

    Phase 4: concurrent per-request task model so a suspended run
    (awaiting approval) does not block reading the next inbound message
    (approval.respond). Routes ``approval.respond`` to the ApprovalRegistry
    inline; session RPCs inline; everything else spawns a run task,
    one active per session.
    """
    from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY

    store = agent.session_store

    async def handler(ws: ServerConnection) -> None:
        try:
            await ws.send(json.dumps(ACK_FRAME, ensure_ascii=False))
        except Exception:
            return
        send_lock = asyncio.Lock()
        active: dict[str, asyncio.Task] = {}
        skill_tasks: set[asyncio.Task] = set()

        async def send(resp: E2AResponse) -> None:
            async with send_lock:
                try:
                    await ws.send(resp.model_dump_json())
                except Exception:
                    log.debug("send on closed connection, dropping %s", resp.request_id)

        async def run_task(envelope: E2AEnvelope) -> None:
            global _inflight_count
            params = envelope.params or {}
            request = AgentRequest(
                session_id=envelope.session_id or envelope.request_id,
                request_id=envelope.request_id,
                query=params.get("query", ""),
                channel=envelope.channel or "web",
                mode=params.get("mode", ""),
            )
            _inflight_count += 1
            try:
                async for frame in agent.run(request):
                    await send(frame)
            except Exception as exc:
                log.exception("agent loop failed for %s: %s", envelope.request_id, exc)
                await send(E2AResponse(
                    request_id=envelope.request_id, is_final=True, status="failed",
                    response_kind="e2a.error", body={"error": str(exc)}))
            finally:
                _inflight_count -= 1

        try:
            async for raw in ws:
                try:
                    envelope = E2AEnvelope.model_validate_json(raw)
                except Exception as exc:
                    await send(E2AResponse(request_id="?", status="failed",
                        response_kind="e2a.error", body={"error": str(exc)}))
                    continue
                if envelope.method == "approval.respond":
                    await APPROVAL_REGISTRY.handle_respond(envelope, send)
                    continue
                if envelope.method == "approval.check_pending":
                    pending = APPROVAL_REGISTRY.get_pending(envelope.session_id or "")
                    await send(E2AResponse(
                        request_id=envelope.request_id, sequence=0, is_final=True,
                        status="succeeded", response_kind="e2a.result",
                        body={"type": "approval.check_pending", "pending": pending},
                    ))
                    continue
                if handles_session_rpc(envelope.method):
                    async for frame in dispatch_session_rpc(envelope, store):
                        await send(frame)
                    continue
                if handles_skill_rpc(envelope.method):
                    if envelope.method == "skills.list_local":
                        async for frame in dispatch_skill_rpc(envelope):
                            await send(frame)
                    else:
                        # search/install: 非内联,后台任务;完成后 send() 延迟发一个 e2a.result。
                        # 不阻塞读循环(慢 GitHub 网络)。成功信号 = 该延迟帧。
                        task = asyncio.create_task(run_skill_rpc(envelope, send, get_skillnet_client()))
                        skill_tasks.add(task)
                        task.add_done_callback(skill_tasks.discard)
                    continue
                session_id = envelope.session_id or envelope.request_id
                cur = active.get(session_id)
                if cur is not None and not cur.done():
                    await send(E2AResponse(
                        request_id=envelope.request_id, is_final=True, status="failed",
                        response_kind="e2a.error",
                        body={"error": "a request is already in progress for this session"}))
                    continue
                task = asyncio.create_task(run_task(envelope))
                active[session_id] = task
                task.add_done_callback(lambda t, session_id=session_id: active.pop(session_id, None) if active.get(session_id) is t else None)
        finally:
            for t in list(active.values()):
                t.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            active.clear()
            for t in list(skill_tasks):
                t.cancel()
            await asyncio.gather(*skill_tasks, return_exceptions=True)
            skill_tasks.clear()
            APPROVAL_REGISTRY.cancel_all()

    return handler


async def main() -> None:
    from twinkle.agentserver.memory.dreaming import start_dreaming
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import LoggingHook, MemoryHook, PermissionHook, RetryHook, SkillHook
    from twinkle.workspace import ensure_workspace_dir

    ensure_workspace_dir()
    store = session_store()
    engine = permission_engine()
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL, timeout=LLM_TIMEOUT)
    agent = create_agent(store, hooks=[PermissionHook(engine), SkillHook(), MemoryHook(), LoggingHook(), RetryHook()], llm=llm)
    handler = ws_handler(agent)
    dreaming_task = start_dreaming(llm, _get_inflight_count)  # 后台记忆整理（opt-in 默认关）
    if dreaming_task is not None:
        log.info("Dreaming background task started")
    log.info("AgentServer listening on %s:%s", AGENTSERVER_HOST, AGENTSERVER_PORT)
    async with serve(handler, AGENTSERVER_HOST, AGENTSERVER_PORT):
        await asyncio.Future()  # run forever
