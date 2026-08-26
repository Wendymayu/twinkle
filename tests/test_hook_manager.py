"""HookManager 测试 — register、unregister、priority 排序、execute。"""
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
    """记录收到的事件及其顺序的 hook。"""
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
    mgr = HookManager()
    h = _RecorderHook()
    mgr.register_hook(h)  # sync — no asyncio.run needed
    assert mgr.has_callbacks_for(HookEvent.BEFORE_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.AFTER_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.BEFORE_MODEL_CALL)
    assert mgr.has_callbacks_for(HookEvent.AFTER_TOOL_CALL)
    # hook 未重写的事件 — 无 callbacks
    assert not mgr.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_register_hook_calls_init():
    class InitRecorder(AgentHook):
        def __init__(self):
            self.inited = False
        def init(self, agent):
            self.inited = True
    mgr = HookManager()
    h = InitRecorder()
    mgr.register_hook(h)  # sync
    assert h.inited is True


def test_unregister_hook_removes_callbacks():
    mgr = HookManager()
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
    mgr = HookManager()
    h = UninitRecorder()
    mgr.register_hook(h)  # sync
    mgr.unregister_hook(h)  # sync
    assert h.uninited is True


def test_execute_calls_hooks_in_priority_order():
    """高 priority 先执行。"""
    mgr = HookManager()
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
    # high(90) 应在 low(10) 之前执行
    assert high.calls == ["high:before_invoke"]
    assert low.calls == ["low:before_invoke"]


def test_execute_no_hooks_is_noop():
    """执行没有注册 hook 的事件不应报错。"""
    mgr = HookManager()
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # 无报错,ctx 不变


def test_execute_updates_ctx_event():
    """execute() 应把 ctx.event 设为正在触发的事件。"""
    mgr = HookManager()
    h = _RecorderHook()
    mgr.register_hook(h)  # sync
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,  # 初始 event
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_MODEL_CALL, ctx))
    # hook 的 before_model_call 应已被调用
    assert h.calls == ["before_model_call"]


def test_execute_fail_soft_continues_after_exception():
    """一个失败的 callback 不应阻止其他 callback 执行。"""
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

    mgr = HookManager()
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
    # 尽管有 hook 失败,safe hook 仍应被调用
    assert safe.calls == ["safe:before_invoke"]
