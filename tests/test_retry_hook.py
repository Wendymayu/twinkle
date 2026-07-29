"""Tests for RetryHook — transient-exception retry for model + tool calls.

RetryHook implements both on_model_exception and on_tool_exception: it asks the
loop to retry (once) only when the exception is transient AND this is the first
attempt. Non-transient errors and second-attempt failures are left to propagate.
"""
from __future__ import annotations

import asyncio

import httpx
import openai

from twinkle.agentserver.hooks.base import (
    HookContext,
    HookEvent,
    ModelCallInputs,
)
from twinkle.agentserver.hooks.builtin.retry_hook import (
    RetryHook,
    TRANSIENT_EXCEPTIONS,
    is_transient,
)


def _ctx(exc, retry_attempt=0, event=HookEvent.ON_MODEL_EXCEPTION):
    return HookContext(
        agent=None,
        event=event,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
        exception=exc,
        retry_attempt=retry_attempt,
    )


def test_is_transient_true_for_timeout_and_transport_errors():
    assert is_transient(asyncio.TimeoutError())
    assert is_transient(httpx.ConnectError("net down"))
    assert is_transient(httpx.ReadTimeout("slow"))


def test_is_transient_false_for_business_errors():
    assert not is_transient(ValueError("bad arg"))
    assert not is_transient(KeyError("missing"))
    assert not is_transient(RuntimeError("boom"))


def test_transient_set_includes_openai_transient_types():
    # openai exception constructors need request/response objects — assert by
    # type membership rather than constructing instances.
    assert openai.APIConnectionError in TRANSIENT_EXCEPTIONS
    assert openai.APITimeoutError in TRANSIENT_EXCEPTIONS
    assert openai.RateLimitError in TRANSIENT_EXCEPTIONS
    assert openai.InternalServerError in TRANSIENT_EXCEPTIONS


def test_retry_hook_requests_retry_for_transient_first_attempt():
    hook = RetryHook()
    ctx = _ctx(asyncio.TimeoutError(), retry_attempt=0)
    asyncio.run(hook.on_model_exception(ctx))
    assert ctx.consume_retry_request() is not None


def test_retry_hook_skips_retry_on_second_attempt():
    hook = RetryHook()
    ctx = _ctx(asyncio.TimeoutError(), retry_attempt=1)
    asyncio.run(hook.on_model_exception(ctx))
    assert ctx.consume_retry_request() is None


def test_retry_hook_skips_retry_for_non_transient():
    hook = RetryHook()
    ctx = _ctx(ValueError("bad"), retry_attempt=0)
    asyncio.run(hook.on_model_exception(ctx))
    assert ctx.consume_retry_request() is None


def test_retry_hook_handles_tool_exception_too():
    hook = RetryHook()
    ctx = _ctx(httpx.ConnectError("net"), retry_attempt=0,
              event=HookEvent.ON_TOOL_EXCEPTION)
    asyncio.run(hook.on_tool_exception(ctx))
    assert ctx.consume_retry_request() is not None
