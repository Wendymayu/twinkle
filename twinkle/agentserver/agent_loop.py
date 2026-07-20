"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).

Twinkle is stream-only; run_unary has been removed.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.registry import ToolRegistry
from twinkle.e2a.models import E2AEnvelope, E2AResponse

MAX_STEPS = 8


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolRegistry,
        memory: LongTermMemory,
    ) -> None:
        self._llm = llm
        self._store = store
        self._tools = tools
        self._memory = memory

    async def run_stream(self, env: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        session_id = env.session_id
        query = (env.params or {}).get("query", "")
        self._store.append(session_id, {"role": "user", "content": query})
        # long-term memory stub: recall is a no-op in Phase 1; shape preserved.
        self._memory.recall(query)

        seq = 0
        full_text = ""
        for _step in range(MAX_STEPS):
            msgs = self._store.get_messages(session_id)
            async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
                if isinstance(ev, TextDelta):
                    full_text += ev.content
                    yield E2AResponse(
                        request_id=env.request_id,
                        sequence=seq,
                        is_final=False,
                        status="in_progress",
                        response_kind="e2a.chunk",
                        body={"result": {"content": ev.content}},
                    )
                    seq += 1
                elif isinstance(ev, Finish):
                    self._store.append(session_id, ev.assistant_message)
                    tcs = ev.assistant_message.get("tool_calls")
                    if ev.finish_reason == "tool_calls" and tcs:
                        for tc in tcs:
                            name = tc["function"]["name"]
                            try:
                                args = json.loads(tc["function"]["arguments"] or "{}")
                            except Exception:
                                args = {}
                            result = await self._tools.execute(name, args)
                            self._store.append(
                                session_id,
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                },
                            )
                        continue  # re-ask model with tool results
                    yield E2AResponse(
                        request_id=env.request_id,
                        sequence=seq,
                        is_final=True,
                        status="succeeded",
                        response_kind="e2a.complete",
                        body={"result": {"content": full_text}},
                    )
                    return
        # exceeded max_steps without converging
        yield E2AResponse(
            request_id=env.request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={MAX_STEPS}"},
        )

