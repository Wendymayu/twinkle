"""RetryHook 的测试 — 针对瞬时异常的 model + tool 调用重试。

RetryHook 同时实现 on_model_exception 和 on_tool_exception：仅当异常为瞬时
且为第一次尝试时，才请求 loop 重试（一次）。非瞬时错误与第二次尝试的失败
原样传播。
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
    # openai 的异常构造器需要 request/response 对象——这里按类型
    # 归属断言，而非构造实例。
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
