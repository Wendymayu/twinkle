"""Instrument LLMClient.stream -> gen_ai.chat span."""
from __future__ import annotations

import json
import time

from opentelemetry.trace import Status, StatusCode

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.observability import attributes as A
from twinkle.observability.context import current_request_context, increment_llm_counter
from twinkle.observability.usage import read_usage_token

_TRUNC_LIMIT = 4096


def _trunc(s: str) -> str:
    return s if len(s) <= _TRUNC_LIMIT else s[:_TRUNC_LIMIT] + "..."


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
    # usage may be a dict (fakes/tests) or a pydantic object (real openai SDK
    # CompletionUsage has no .get); read_usage_token handles both — using
    # usage.get() here used to raise AttributeError mid-span and break the
    # whole agent invoke.
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
                span.set_attribute(A.GEN_AI_INPUT_MESSAGES, _trunc(json.dumps(messages)))
                if tools:
                    span.set_attribute(A.GEN_AI_TOOL_DEFINITIONS, _trunc(json.dumps(tools)))
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
                        # Finish is the terminal event. End the span NOW
                        # (before yielding) so it's exported even if the
                        # caller abandons the generator — the agent loop
                        # returns from inside its `async for` on the final
                        # turn, which would otherwise leave this span
                        # unended and unexported.
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
                                _trunc(json.dumps([ev.assistant_message])),
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
