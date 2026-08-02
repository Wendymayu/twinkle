"""Tests for ContextOverflowRecoveryHook — 413 detection, token parsing,
forced compression + retry, circuit-break, count reset."""

import asyncio
import httpx

from twinkle.agentserver.compression import estimate_tokens
from twinkle.agentserver.hooks.base import ModelCallInputs
from twinkle.agentserver.hooks.builtin.context_overflow_recovery_hook import (
    ContextOverflowRecoveryHook,
    _is_context_overflow_error,
    _parse_token_limits,
)


class _FakeLLM:
    """Fake LLM for compress_messages — yields fixed summary."""
    async def stream(self, messages, tools):
        from twinkle.agentserver.llm_client import TextDelta
        yield TextDelta("摘要")


class _Ctx:
    """Minimal ctx stub: hook touches ctx.inputs.messages, ctx.exception, ctx.extra."""
    def __init__(self, messages, exception=None):
        self.inputs = ModelCallInputs(messages=messages, tools=[])
        self.exception = exception
        self.extra = {}
        self._retry_request = None
        self._force_finish_request = None

    def request_retry(self, delay=0):
        self._retry_request = delay

    def request_force_finish(self, result=None):
        self._force_finish_request = result


# --- _is_context_overflow_error tests ---

class _Exc413(Exception):
    status_code = 413

class _Exc400(Exception):
    status_code = 400

class _ExcNoStatus(Exception):
    pass


def test_detects_413_status_code():
    assert _is_context_overflow_error(_Exc413("some error")) is True


def test_detects_400_with_context_length_exceeded():
    assert _is_context_overflow_error(_Exc400("context_length_exceeded")) is True


def test_detects_400_with_prompt_too_long():
    assert _is_context_overflow_error(_Exc400("prompt is too long: 10000 tokens > 8000")) is True


def test_detects_400_with_input_too_long():
    assert _is_context_overflow_error(_Exc400("input too long for model")) is True


def test_detects_400_without_overflow_keyword():
    assert _is_context_overflow_error(_Exc400("invalid parameter")) is False


def test_detects_no_status_with_overflow_keyword():
    assert _is_context_overflow_error(_ExcNoStatus("context_length_exceeded")) is True


def test_detects_no_status_without_keyword():
    assert _is_context_overflow_error(_ExcNoStatus("rate limit exceeded")) is False


def test_detects_anthropic_prompt_too_long():
    assert _is_context_overflow_error(_ExcNoStatus("prompt is too long: 10000 tokens > 8000")) is True


def test_ignores_rate_limit_error():
    import openai
    exc = openai.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "http://test")),
        body=None,
    )
    assert _is_context_overflow_error(exc) is False


# --- _parse_token_limits tests ---

def test_parse_anthropic_format():
    actual, limit = _parse_token_limits(Exception("prompt is too long: 10000 tokens > 8000"))
    assert actual == 10000
    assert limit == 8000


def test_parse_openai_format():
    actual, limit = _parse_token_limits(Exception("This model's maximum context length is 128000 tokens"))
    assert actual is None
    assert limit == 128000


def test_parse_no_match():
    actual, limit = _parse_token_limits(Exception("some unknown error"))
    assert actual is None
    assert limit is None


# --- Hook behavior tests ---

def _big_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    return msgs


def test_compresses_and_requests_retry_on_413():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    big = _big_messages()
    ctx = _Ctx(big, exception=_Exc413("overflow"))

    asyncio.run(hook.on_model_exception(ctx))

    # Messages should be compressed (fewer tokens)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    # Retry was requested
    assert ctx._retry_request is not None


def test_no_retry_on_non_overflow_error():
    hook = ContextOverflowRecoveryHook(llm=_FakeLLM())
    ctx = _Ctx([{"role": "system", "content": "s"}], exception=_ExcNoStatus("rate limit"))

    asyncio.run(hook.on_model_exception(ctx))

    # No retry requested
    assert ctx._retry_request is None


def test_circuit_break_after_max_attempts():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=2,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    # Simulate 2 consecutive overflow errors
    ctx1 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx1))
    ctx2 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx2))
    # 3rd should trigger circuit break
    ctx3 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx3))

    # Circuit break: requests force_finish so user gets a graceful message (not another 413)
    assert ctx3._force_finish_request is not None
    assert "上下文持续溢出" in ctx3._force_finish_request


def test_resets_count_on_success():
    hook = ContextOverflowRecoveryHook(llm=_FakeLLM(), max_recovery_attempts=3)
    # Simulate 1 overflow
    ctx1 = _Ctx(_big_messages(), exception=_Exc413("overflow"))
    asyncio.run(hook.on_model_exception(ctx1))
    assert hook._consecutive_overflow_count == 1

    # Successful model call resets count
    ctx2 = _Ctx([{"role": "system", "content": "s"}], exception=None)
    asyncio.run(hook.after_model_call(ctx2))
    assert hook._consecutive_overflow_count == 0


def test_uses_parsed_limit_for_threshold():
    hook = ContextOverflowRecoveryHook(
        llm=_FakeLLM(), max_recovery_attempts=3,
        aggressive_keep_recent=2, threshold_ratio=0.85,
    )
    big = _big_messages()
    # Anthropic format with small limit: "prompt is too long: 10000 tokens > 1000"
    # threshold_override = int(1000 * 0.85) = 850, which is below the ~1377 tokens
    ctx = _Ctx(big, exception=_ExcNoStatus("prompt is too long: 10000 tokens > 1000"))

    asyncio.run(hook.on_model_exception(ctx))

    # Compression should have been applied (result is shorter)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert ctx._retry_request is not None
