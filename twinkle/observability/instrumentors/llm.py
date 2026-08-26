"""Instrument LLMClient.stream -> gen_ai.chat span。"""
from __future__ import annotations

import json
import os
import time

from opentelemetry.trace import Status, StatusCode

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.observability import attributes as A
from twinkle.observability.context import current_request_context, increment_llm_counter
from twinkle.observability.usage import read_usage_token

_DEFAULT_ATTR_LIMIT = 0  # 0 = no truncation (full capture) by default


def _attr_limit() -> int:
    """Span-attribute 截断上限，来自 TWINKLE_OBS_ATTR_LIMIT（默认 0 = 全量）。

    默认不截断，使调试时 trace 的 input/output 完全可见；设为正整数
    （如 4096）可分别限制 gen_ai.input.messages / tool.definitions /
    output.messages / tool.arguments / tool.result。懒读取，使 env 变更
    （及测试）无需重新 import 即生效。
    """
    raw = os.getenv("TWINKLE_OBS_ATTR_LIMIT", str(_DEFAULT_ATTR_LIMIT))
    try:
        n = int((raw or "").strip())
    except (TypeError, ValueError):
        n = _DEFAULT_ATTR_LIMIT
    return max(0, n)


def _trunc(s: str) -> str:
    limit = _attr_limit()
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + "..."


def _stamp_ctx(span) -> None:
    ctx = current_request_context()
    if ctx is None:
        return
    if ctx.request_id is not None:
        span.set_attribute(A.TWINKLE_REQUEST_ID, ctx.request_id)
    if ctx.session_id is not None:
        span.set_attribute(A.TWINKLE_SESSION_ID, ctx.session_id)


def _record_usage_attrs(span, usage) -> None:
    if not usage:
        return
    # usage 可能是 dict（fakes/tests）或 pydantic 对象（真实 openai SDK
    # CompletionUsage 无 .get）；read_usage_token 兼顾两者——此处用
    # usage.get() 曾经会在 span 中途抛 AttributeError 并中断整个
    # agent invoke。
    inp = read_usage_token(usage, "prompt_tokens", "input_tokens")
    out = read_usage_token(usage, "completion_tokens", "output_tokens")
    tot = read_usage_token(usage, "total_tokens")
    if inp is not None:
        span.set_attribute(A.GEN_AI_USAGE_INPUT_TOKENS, int(inp))
    if out is not None:
        span.set_attribute(A.GEN_AI_USAGE_OUTPUT_TOKENS, int(out))
    if tot is not None:
        span.set_attribute(A.GEN_AI_USAGE_TOTAL_TOKENS, int(tot))


def instrument_llm(tracer, metrics, cfg, *, llm_cls=None) -> bool:
    if llm_cls is None:
        from twinkle.agentserver.llm_client import LLMClient as llm_cls

    def factory(original):
        async def traced(self, messages, tools):
            increment_llm_counter()
            span = tracer.start_span(A.SPAN_GEN_AI_CHAT)
            _stamp_ctx(span)
            model = getattr(self, "_model", "unknown")
            span.set_attribute(A.GEN_AI_SYSTEM, "openai")
            span.set_attribute(A.GEN_AI_REQUEST_MODEL, model)
            span.set_attribute(A.GEN_AI_OPERATION_NAME, "chat")
            try:
                span.set_attribute(A.GEN_AI_INPUT_MESSAGES, _trunc(json.dumps(messages, ensure_ascii=False)))
                if tools:
                    span.set_attribute(A.GEN_AI_TOOL_DEFINITIONS, _trunc(json.dumps(tools, ensure_ascii=False)))
            except Exception:
                pass
            start = time.perf_counter()
            first_token_ts = None
            ended = False
            try:
                async for ev in original(self, messages, tools):
                    if isinstance(ev, TextDelta):
                        if first_token_ts is None:
                            first_token_ts = time.perf_counter()
                    elif isinstance(ev, Finish):
                        # Finish 是终止事件。立即结束 span（在 yield
                        # 之前），使其即便 caller 放弃 generator 也会被
                        # 导出——agent loop 在最后一轮从其 `async for`
                        # 内部 return，否则会令此 span 未结束、未导出。
                        if first_token_ts is not None:
                            span.set_attribute(
                                A.GEN_AI_STREAMING_FIRST_TOKEN_MS,
                                int((first_token_ts - start) * 1000),
                            )
                        span.set_attribute(A.GEN_AI_RESPONSE_FINISH_REASON, ev.finish_reason)
                        _record_usage_attrs(span, ev.usage)
                        try:
                            span.set_attribute(
                                A.GEN_AI_OUTPUT_MESSAGES,
                                _trunc(json.dumps([ev.assistant_message], ensure_ascii=False)),
                            )
                        except Exception:
                            pass
                        metrics.record_token_usage(ev.usage, model)
                        metrics.record_llm_duration(model, time.perf_counter() - start)
                        span.end()
                        ended = True
                    yield ev
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.record_exception(exc)
                raise
            finally:
                if not ended:
                    span.end()

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(llm_cls, "stream", factory)
