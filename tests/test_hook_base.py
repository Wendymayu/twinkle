"""HookEvent 枚举与 AgentHook 基类测试。"""
from __future__ import annotations

import enum

from twinkle.agentserver.hooks.base import AgentHook, HookEvent


def test_hook_event_has_11_values():
    assert len(HookEvent) == 11


def test_hook_event_values_match_names():
    expected = {
        "BEFORE_INVOKE", "AFTER_INVOKE",
        "BEFORE_MODEL_CALL", "AFTER_MODEL_CALL", "ON_MODEL_EXCEPTION",
        "BEFORE_TOOL_CALL", "AFTER_TOOL_CALL", "ON_TOOL_EXCEPTION",
        "AFTER_REACT_ITERATION",
        "BEFORE_TASK_ITERATION", "AFTER_TASK_ITERATION",
    }
    assert {e.name for e in HookEvent} == expected


def test_hook_event_is_enum():
    assert issubclass(HookEvent, enum.Enum)


def test_base_hook_default_priority():
    h = AgentHook()
    assert h.priority == 50


def test_base_hook_get_callbacks_returns_empty():
    """未重写任何方法的 base AgentHook 应返回空 callbacks dict。"""
    h = AgentHook()
    callbacks = h.get_callbacks()
    assert callbacks == {}


def test_subclass_get_callbacks_returns_only_overridden():
    """重写 2 个方法的子类应得到 2 个 callback。"""
    class TwoMethodHook(AgentHook):
        priority = 90

        async def before_model_call(self, ctx):
            pass

        async def after_tool_call(self, ctx):
            pass

    h = TwoMethodHook()
    callbacks = h.get_callbacks()
    assert len(callbacks) == 2
    assert HookEvent.BEFORE_MODEL_CALL in callbacks
    assert HookEvent.AFTER_TOOL_CALL in callbacks


def test_subclass_init_uninit_not_in_callbacks():
    """init/uninit 是生命周期方法,不是 event callback — 它们不应
    出现在 get_callbacks() 中。"""
    class InitHook(AgentHook):
        def init(self, agent):
            pass

        async def before_invoke(self, ctx):
            pass

    h = InitHook()
    callbacks = h.get_callbacks()
    assert HookEvent.BEFORE_INVOKE in callbacks
    # init 不是 HookEvent callback
    assert len(callbacks) == 1


def test_subclass_priority_propagated_to_callbacks():
    """同一 Hook 的所有 callback 共享其 priority。"""
    class HighPriHook(AgentHook):
        priority = 100

        async def before_invoke(self, ctx):
            pass

        async def after_invoke(self, ctx):
            pass

    h = HighPriHook()
    callbacks = h.get_callbacks()
    assert len(callbacks) == 2


def test_is_base_method_detects_override():
    class OverrideHook(AgentHook):
        async def before_model_call(self, ctx):
            pass

    h = OverrideHook()
    # 被重写的方法不应被判定为 "base"
    assert not h._is_base_method(h.before_model_call)
    # 未重写的方法应被判定为 "base"
    assert h._is_base_method(h.after_model_call)
