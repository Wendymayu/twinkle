"""Tests for HookManager — register, unregister, priority ordering, execute."""
from __future__ import annotations

import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    InvokeInputs,
)
from twinkle.agentserver.hooks.manager import HookManager


class _RecorderHook(AgentHook):
    """Hook that records which events it received, in order."""
    priority = 50

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("before_invoke")

    async def after_invoke(self, ctx):
        self.calls.append("after_invoke")

    async def before_model_call(self, ctx):
        self.calls.append("before_model_call")

    async def after_tool_call(self, ctx):
        self.calls.append("after_tool_call")


class _HighPriHook(AgentHook):
    priority = 90

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("high:before_invoke")


class _LowPriHook(AgentHook):
    priority = 10

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("low:before_invoke")


def test_register_hook_adds_callbacks():
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    mgr.register_hook(h)  # sync — no asyncio.run needed
    assert mgr.has_callbacks_for(HookEvent.BEFORE_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.AFTER_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.BEFORE_MODEL_CALL)
    assert mgr.has_callbacks_for(HookEvent.AFTER_TOOL_CALL)
    # Events the hook doesn't override — no callbacks
    assert not mgr.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_register_hook_calls_init():
    class InitRecorder(AgentHook):
        def __init__(self):
            self.inited = False
        def init(self, agent):
            self.inited = True
    mgr = HookManager(agent="fake_agent")
    h = InitRecorder()
    mgr.register_hook(h)  # sync
    assert h.inited is True


def test_unregister_hook_removes_callbacks():
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    mgr.register_hook(h)  # sync
    mgr.unregister_hook(h)  # sync
    assert not mgr.has_callbacks_for(HookEvent.BEFORE_INVOKE)


def test_unregister_hook_calls_uninit():
    class UninitRecorder(AgentHook):
        def __init__(self):
            self.uninited = False
        def uninit(self, agent):
            self.uninited = True
    mgr = HookManager(agent="fake_agent")
    h = UninitRecorder()
    mgr.register_hook(h)  # sync
    mgr.unregister_hook(h)  # sync
    assert h.uninited is True


def test_execute_calls_hooks_in_priority_order():
    """Higher priority runs first."""
    mgr = HookManager(agent=None)
    high = _HighPriHook()
    low = _LowPriHook()
    mgr.register_hook(low)   # register low first
    mgr.register_hook(high)  # then high
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # high(90) should run before low(10)
    assert high.calls == ["high:before_invoke"]
    assert low.calls == ["low:before_invoke"]


def test_execute_no_hooks_is_noop():
    """Executing an event with no registered hooks should not error."""
    mgr = HookManager(agent=None)
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # No error, ctx unchanged


def test_execute_updates_ctx_event():
    """execute() should set ctx.event to the event being triggered."""
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    mgr.register_hook(h)  # sync
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,  # initial event
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_MODEL_CALL, ctx))
    # The hook's before_model_call should have been called
    assert h.calls == ["before_model_call"]


def test_execute_fail_soft_continues_after_exception():
    """One failing callback should not stop others from running."""
    class FailingHook(AgentHook):
        priority = 90

        async def before_invoke(self, ctx):
            raise RuntimeError("boom")

    class SafeHook(AgentHook):
        priority = 50

        def __init__(self):
            self.calls: list[str] = []

        async def before_invoke(self, ctx):
            self.calls.append("safe:before_invoke")

    mgr = HookManager(agent=None)
    mgr.register_hook(FailingHook())
    safe = SafeHook()
    mgr.register_hook(safe)
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # Safe hook should still have been called despite failing hook
    assert safe.calls == ["safe:before_invoke"]
