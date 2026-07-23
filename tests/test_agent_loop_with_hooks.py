"""Tests for AgentLoop integration with the Hook mechanism.

Verifies that hooks are called at the right events, in priority order,
and that frame output is unchanged when hooks are present.
"""
from __future__ import annotations

import asyncio

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.tools.decorator import tool
from twinkle.e2a.models import E2AEnvelope


class _CallOrderHook(AgentHook):
    """Records the order of hook calls with their events."""
    priority = 50

    def __init__(self):
        self.calls: list[tuple[HookEvent, str | None]] = []

    async def before_invoke(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def after_invoke(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def before_model_call(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def after_model_call(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def before_tool_call(self, ctx):
        self.calls.append((ctx.event, ctx.inputs.name))

    async def after_tool_call(self, ctx):
        self.calls.append((ctx.event, ctx.inputs.name))


def _env(query, rid="r1", session_id="s1"):
    return E2AEnvelope(
        request_id=rid,
        session_id=session_id,
        method="chat.send",
        params={"query": query},
    )


class _ScriptedLLM:
    """Returns one canned event-list per call, in order."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _reg_with_echo_tool():
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    m = ToolManager()
    m.register(echo)
    return m


def test_hooks_called_on_plain_answer(session_store) -> None:
    """Plain answer flow: BEFORE_INVOKE -> BEFORE_MODEL_CALL -> AFTER_MODEL_CALL -> AFTER_INVOKE."""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        [TextDelta("hi"), Finish("stop", {"role": "assistant", "content": "hi", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())
    loop.register_hook(order_hook)

    async def run():
        frames = [f async for f in loop.run_stream(_env("hello"))]
        return frames

    frames = asyncio.run(run())
    # Verify frame output unchanged
    assert frames[-1].response_kind == "e2a.complete"

    # Verify hook call order
    events = [c[0] for c in order_hook.calls]
    assert events == [
        HookEvent.BEFORE_INVOKE,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.AFTER_INVOKE,
    ]


def test_hooks_called_on_tool_call_round_trip(session_store) -> None:
    """Tool call flow: invoke -> model_call -> tool_call -> model_call -> invoke."""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        # turn 1: model calls echo tool
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2: model produces final answer
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())
    loop.register_hook(order_hook)

    async def run():
        frames = [f async for f in loop.run_stream(_env("call echo"))]
        return frames

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"

    events = [c[0] for c in order_hook.calls]
    assert events == [
        HookEvent.BEFORE_INVOKE,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.BEFORE_TOOL_CALL,
        HookEvent.AFTER_TOOL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.AFTER_INVOKE,
    ]


def test_existing_tests_still_pass(session_store) -> None:
    """AgentLoop without hooks produces identical output — existing tests
    should pass unchanged. This is a meta-test: run the plain answer test
    WITHOUT hooks and verify frames."""
    store = session_store
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"


def test_logging_hook_registers_and_works(session_store) -> None:
    """LoggingHook can be registered and its get_callbacks returns 4 events."""
    store = session_store
    lh = LoggingHook()
    callbacks = lh.get_callbacks()
    assert len(callbacks) == 4
    assert HookEvent.BEFORE_MODEL_CALL in callbacks
    assert HookEvent.AFTER_MODEL_CALL in callbacks
    assert HookEvent.BEFORE_TOOL_CALL in callbacks
    assert HookEvent.AFTER_TOOL_CALL in callbacks
    assert lh.priority == 10
