"""@hook 装饰器测试 — before/after/exception/force_finish。"""
from __future__ import annotations

import asyncio

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


# --- 辅助:一个带 HookManager 的最小 "agent" --- #

class _FakeAgent:
    def __init__(self):
        self._hook_manager = HookManager()
        self.call_log: list[str] = []


class _RecorderHook(AgentHook):
    """记录收到的事件。"""
    priority = 50

    def __init__(self):
        self.calls: list[str] = []

    async def before_model_call(self, ctx):
        self.calls.append("before_model_call")

    async def after_model_call(self, ctx):
        self.calls.append("after_model_call")

    async def on_model_exception(self, ctx):
        self.calls.append("on_model_exception")


def test_hook_decorator_triggers_before_then_body_then_after():
    """@hook(BEFORE, AFTER) 包装方法:before -> body -> after。"""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hook_manager.register_hook(rec)

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL)
    async def do_work(self, ctx):
        self.call_log.append("body")
        return "done"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(do_work(agent, ctx))
    assert result == "done"
    assert agent.call_log == ["body"]
    assert rec.calls == ["before_model_call", "after_model_call"]


def test_hook_decorator_on_exception_triggers_exception_hook():
    """方法抛异常时调用 on_exception hook。"""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hook_manager.register_hook(rec)

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def failing_work(self, ctx):
        self.call_log.append("body")
        raise ValueError("boom")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(failing_work(agent, ctx))
    except ValueError:
        pass
    assert agent.call_log == ["body"]
    assert rec.calls == ["before_model_call", "on_model_exception"]
    # 异常时不应调用 after
    assert "after_model_call" not in rec.calls


def test_hook_decorator_force_finish_skips_body():
    """若 before-hook 设置了 force_finish,方法体被跳过。"""
    class ForceFinishHook(AgentHook):
        priority = 100
        async def before_model_call(self, ctx):
            ctx.request_force_finish(result="blocked")

    agent = _FakeAgent()
    agent._hook_manager.register_hook(ForceFinishHook())

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL)
    async def do_work(self, ctx):
        self.call_log.append("body")  # 不应执行
        return "done"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(do_work(agent, ctx))
    assert result == "blocked"
    assert agent.call_log == []  # body was skipped


def test_hook_decorator_interrupt_propagates_immediately():
    """@hook 装饰的方法内抛出 HookInterrupt 会直接向上传播,
    不触发 on_exception。"""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hook_manager.register_hook(rec)

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def interrupting_work(self, ctx):
        raise HookInterrupt("need approval")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(interrupting_work(agent, ctx))
    except HookInterrupt:
        pass
    # HookInterrupt 不应触发 on_model_exception
    assert "on_model_exception" not in rec.calls


def test_hook_decorator_cancelled_error_propagates_immediately():
    """asyncio.CancelledError 穿过 @hook 向上传播,
    不触发 on_exception 或 after hook。"""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hook_manager.register_hook(rec)

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def cancelling_work(self, ctx):
        raise asyncio.CancelledError()

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(cancelling_work(agent, ctx))
    except asyncio.CancelledError:
        pass
    # CancelledError 应立即向上传播 — 不触发 exception 或 after hook
    assert "on_model_exception" not in rec.calls
    assert "after_model_call" not in rec.calls
    # 只有 before hook 被触发
    assert rec.calls == ["before_model_call"]


def test_hook_decorator_on_exception_none_propagates_without_hooks():
    """当 on_exception=None 且方法抛异常时,异常直接向上传播
    — 不触发 exception hook,也不调用 after。"""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hook_manager.register_hook(rec)

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=None)
    async def failing_no_exception_hook(self, ctx):
        self.call_log.append("body")
        raise ValueError("unhandled")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(failing_no_exception_hook(agent, ctx))
    except ValueError as e:
        assert str(e) == "unhandled"
    assert agent.call_log == ["body"]
    # before hook 触发,但无 exception hook 也无 after hook
    assert rec.calls == ["before_model_call"]
    assert "on_model_exception" not in rec.calls
    assert "after_model_call" not in rec.calls


def test_after_event_receives_tool_result():
    """装饰器在 after-event 之前把方法返回值存入 ctx.extra['_tool_result']。"""
    results = {}

    class SpyHook(AgentHook):
        priority = 50
        async def after_tool_call(self, ctx: HookContext) -> None:
            results["tool_result"] = ctx.extra.get("_tool_result")

    class FakeLoop:
        def __init__(self):
            self._hook_manager = HookManager()
            self._hook_manager.register_hook(SpyHook())

    loop = FakeLoop()
    ctx = HookContext(
        agent=loop,
        event=HookEvent.BEFORE_TOOL_CALL,
        inputs=ToolCallInputs(name="test", args={}, tool_call_id="tc1"),
        session_id=None,
        request_id=None,
        extra={},
    )

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL)
    async def tool_method(self, ctx):
        return "tool-output-42"

    asyncio.run(tool_method(loop, ctx))
    assert results["tool_result"] == "tool-output-42"
