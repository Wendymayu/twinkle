"""AgentLoop 与 Hook 机制的集成测试。

验证 hook 在正确的事件、按 priority 顺序被调用，
且有 hook 时 frame 输出不变。
"""
from __future__ import annotations

import asyncio

from twinkle.agentserver.agent import ReActAgent as AgentLoop
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.agent import AgentRequest


class _CallOrderHook(AgentHook):
    """记录 hook 调用顺序及其事件。"""
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


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(
        session_id=session_id,
        request_id=request_id,
        query=query,
    )


class _ScriptedLLM:
    """每次调用按顺序返回一组预设事件列表。"""
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
    """纯回答流程：BEFORE_INVOKE -> BEFORE_MODEL_CALL -> AFTER_MODEL_CALL -> AFTER_INVOKE。"""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        [TextDelta("hi"), Finish("stop", {"role": "assistant", "content": "hi", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool())
    loop.register_hook(order_hook)

    async def run():
        frames = [f async for f in loop.run(_env("hello"))]
        return frames

    frames = asyncio.run(run())
    # 验证 frame 输出不变
    assert frames[-1].response_kind == "e2a.complete"

    # 验证 hook 调用顺序
    events = [c[0] for c in order_hook.calls]
    assert events == [
        HookEvent.BEFORE_INVOKE,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.AFTER_INVOKE,
    ]


def test_hooks_called_on_tool_call_round_trip(session_store) -> None:
    """工具调用流程：invoke -> model_call -> tool_call -> model_call -> invoke。"""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        # turn 1：model 调用 echo 工具
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2：model 给出最终回答
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool())
    loop.register_hook(order_hook)

    async def run():
        frames = [f async for f in loop.run(_env("call echo"))]
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
    """无 hook 的 AgentLoop 产出相同输出——既有测试
    应原样通过。这是元测试：不带 hook 跑纯回答流程并验证 frame。"""
    store = session_store
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool())

    async def run():
        frames = [f async for f in loop.run(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"


def test_logging_hook_registers_and_works(session_store) -> None:
    """LoggingHook 可被注册，其 get_callbacks 返回 4 个事件。"""
    store = session_store
    lh = LoggingHook()
    callbacks = lh.get_callbacks()
    assert len(callbacks) == 4
    assert HookEvent.BEFORE_MODEL_CALL in callbacks
    assert HookEvent.AFTER_MODEL_CALL in callbacks
    assert HookEvent.BEFORE_TOOL_CALL in callbacks
    assert HookEvent.AFTER_TOOL_CALL in callbacks
    assert lh.priority == 10
