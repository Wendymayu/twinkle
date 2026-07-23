"""Tests for HookContext, HookInputs subclasses, and control flow signals."""
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
    ForceFinishRequest,
    TaskIterationInputs,
    ToolCallInputs,
)


def test_hook_context_basic_fields():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    assert ctx.session_id == "s1"
    assert ctx.request_id == "r1"
    assert ctx.event == HookEvent.BEFORE_INVOKE
    assert isinstance(ctx.extra, dict)
    assert ctx.exception is None
    assert ctx.retry_attempt == 0


def test_hook_context_extra_dict_shared_across_access():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    ctx.extra["key1"] = "value1"
    assert ctx.extra["key1"] == "value1"
    # Different ctx.extra dicts are independent
    ctx2 = HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s2",
        request_id="r2",
    )
    assert "key1" not in ctx2.extra


def test_invoke_inputs():
    inp = InvokeInputs(query="hello", envelope=None)
    assert inp.query == "hello"
    assert inp.envelope is None


def test_model_call_inputs():
    inp = ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[{"name": "echo"}])
    assert len(inp.messages) == 1
    assert len(inp.tools) == 1


def test_tool_call_inputs():
    inp = ToolCallInputs(name="echo", args={"text": "hi"}, tool_call_id="tc1")
    assert inp.name == "echo"
    assert inp.args == {"text": "hi"}
    assert inp.tool_call_id == "tc1"


def test_task_iteration_inputs():
    inp = TaskIterationInputs(envelope=None)
    assert inp.envelope is None


def test_request_retry_and_consume():
    ctx = HookContext(
        agent=None,
        event=HookEvent.ON_MODEL_EXCEPTION,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    # Initially no retry request
    assert ctx.consume_retry_request() is None

    # Set retry request
    ctx.request_retry(delay=0.5)
    req = ctx.consume_retry_request()
    assert req is not None
    assert req.delay == 0.5

    # Consumed — second call returns None
    assert ctx.consume_retry_request() is None


def test_request_force_finish_and_consume():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    # Initially no force finish request
    assert ctx.consume_force_finish_request() is None

    # Set force finish request
    ctx.request_force_finish(result="denied")
    ff = ctx.consume_force_finish_request()
    assert ff is not None
    assert ff.result == "denied"

    # Consumed — second call returns None
    assert ctx.consume_force_finish_request() is None


def test_hook_interrupt_exception():
    exc = HookInterrupt(message="need approval", data={"tool": "rm"})
    assert str(exc) == "need approval"
    assert exc.data == {"tool": "rm"}
    # Default data is empty dict
    exc2 = HookInterrupt(message="stop")
    assert exc2.data == {}


def test_hook_interrupt_is_exception():
    assert issubclass(HookInterrupt, Exception)


def test_retry_request_dataclass():
    req = RetryRequest(delay=2.0)
    assert req.delay == 2.0
    req_default = RetryRequest()
    assert req_default.delay == 0


def test_force_finish_request_dataclass():
    ff = ForceFinishRequest(result="blocked")
    assert ff.result == "blocked"
    ff_default = ForceFinishRequest()
    assert ff_default.result is None
