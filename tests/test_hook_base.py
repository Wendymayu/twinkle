"""Tests for HookEvent enum and AgentHook base class."""
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
    """Base AgentHook with no overrides should return empty callbacks dict."""
    h = AgentHook()
    callbacks = h.get_callbacks()
    assert callbacks == {}


def test_subclass_get_callbacks_returns_only_overridden():
    """A subclass that overrides 2 methods should get 2 callbacks."""
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
    """init/uninit are lifecycle methods, not event callbacks — they should
    never appear in get_callbacks()."""
    class InitHook(AgentHook):
        def init(self, agent):
            pass

        async def before_invoke(self, ctx):
            pass

    h = InitHook()
    callbacks = h.get_callbacks()
    assert HookEvent.BEFORE_INVOKE in callbacks
    # init is NOT a HookEvent callback
    assert len(callbacks) == 1


def test_subclass_priority_propagated_to_callbacks():
    """All callbacks from the same Hook share its priority."""
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
    # The overridden method should NOT be detected as "base"
    assert not h._is_base_method(h.before_model_call)
    # A method NOT overridden should be detected as "base"
    assert h._is_base_method(h.after_model_call)
