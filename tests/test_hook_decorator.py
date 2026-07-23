"""Tests for the @hook decorator — before/after/exception/force_finish/retry."""
from __future__ import annotations

import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    RetryRequest,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager


# --- Helper: a minimal "agent" with a HookManager --- #

class _FakeAgent:
    def __init__(self):
        self._hooks = HookManager(self)
        self.call_log: list[str] = []


class _RecorderHook(AgentHook):
    """Records events it receives."""
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
    """@hook(BEFORE, AFTER) wraps a method: before -> body -> after."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hooks.register_hook(rec)

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
    """When method raises, on_exception hook is called."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hooks.register_hook(rec)

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
    # after should NOT be called on exception
    assert "after_model_call" not in rec.calls


def test_hook_decorator_force_finish_skips_body():
    """If a before-hook sets force_finish, the method body is skipped."""
    class ForceFinishHook(AgentHook):
        priority = 100
        async def before_model_call(self, ctx):
            ctx.request_force_finish(result="blocked")

    agent = _FakeAgent()
    agent._hooks.register_hook(ForceFinishHook())

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL)
    async def do_work(self, ctx):
        self.call_log.append("body")  # should NOT execute
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


def test_hook_decorator_retry_re_executes_body():
    """If on_exception hook requests retry, the method body is re-executed."""
    class RetryHook(AgentHook):
        priority = 100
        fail_count = 0

        async def on_model_exception(self, ctx):
            self.fail_count += 1
            if self.fail_count < 2:
                ctx.request_retry(delay=0)

    agent = _FakeAgent()
    agent._hooks.register_hook(RetryHook())

    agent.attempt = 0

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def retryable_work_v2(self, ctx):
        self.attempt += 1
        if self.attempt < 2:
            raise ValueError("temporary failure")
        self.call_log.append(f"body-attempt-{self.attempt}")
        return "success"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(retryable_work_v2(agent, ctx))
    assert result == "success"
    assert agent.call_log == ["body-attempt-2"]


def test_hook_decorator_interrupt_propagates_immediately():
    """HookInterrupt raised inside a @hook-decorated method propagates
    without triggering on_exception."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hooks.register_hook(rec)

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
    # on_model_exception should NOT be called for HookInterrupt
    assert "on_model_exception" not in rec.calls


def test_hook_decorator_cancelled_error_propagates_immediately():
    """asyncio.CancelledError propagates through @hook without triggering
    on_exception or after hooks."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hooks.register_hook(rec)

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
    # CancelledError should propagate immediately — no exception or after hooks
    assert "on_model_exception" not in rec.calls
    assert "after_model_call" not in rec.calls
    # Only the before hook should have fired
    assert rec.calls == ["before_model_call"]


def test_hook_decorator_max_retries_exceeded_boundary():
    """When on_exception keeps requesting retry, the method is executed
    original + 3 retries (4 total), then the exception is re-raised."""
    class AlwaysRetryHook(AgentHook):
        priority = 100
        retry_count = 0

        async def on_model_exception(self, ctx):
            self.retry_count += 1
            ctx.request_retry(delay=0)  # always request retry

    agent = _FakeAgent()
    always_retry = AlwaysRetryHook()
    agent._hooks.register_hook(always_retry)

    agent.exec_count = 0

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def always_failing_work(self, ctx):
        self.exec_count += 1
        raise ValueError("persistent failure")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(always_failing_work(agent, ctx))
    except ValueError as e:
        assert str(e) == "persistent failure"
    # 4 total executions: original (attempt=0) + 3 retries (attempt=1,2,3)
    assert agent.exec_count == 4
    # on_exception was called 4 times (one per failed execution)
    assert always_retry.retry_count == 4


def test_hook_decorator_on_exception_none_propagates_without_hooks():
    """When on_exception=None and the method raises, the exception propagates
    directly — no exception hook is triggered, and after is NOT called."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    agent._hooks.register_hook(rec)

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
    # before hook fires, but no exception hook and no after hook
    assert rec.calls == ["before_model_call"]
    assert "on_model_exception" not in rec.calls
    assert "after_model_call" not in rec.calls
