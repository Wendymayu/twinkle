"""Instrument AgentLoop.run_stream -> twinkle.agent.invoke (root span).

Opens the root span as current so child gen_ai.chat / gen_ai.tool spans
(parent = current) attach under it. Stamps request_id/session_id onto the
ContextVar so child spans can pick them up via _stamp_ctx. Counts LLM calls
via _llm_call_counter to set twinkle.agent.iterations at span end.
"""
from __future__ import annotations

import time

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.context import (
    current_llm_counter,
    reset_llm_counter,
    set_request_context,
)


def instrument_agent(tracer, metrics, cfg, *, agent_cls=None) -> bool:
    if agent_cls is None:
        from twinkle.agentserver.agent_loop import AgentLoop as agent_cls

    def factory(original):
        async def traced(self, envelope):
            req_id = getattr(envelope, "request_id", None)
            sess_id = getattr(envelope, "session_id", None)
            start = time.perf_counter()
            with tracer.start_as_current_span(A.SPAN_AGENT_INVOKE) as span:
                rctx_tok = set_request_context(
                    request_id=req_id, session_id=sess_id, agent_name=type(self).__name__
                )
                ctr_tok = reset_llm_counter()
                span.set_attribute(A.TWINKLE_REQUEST_ID, req_id or "")
                span.set_attribute(A.TWINKLE_SESSION_ID, sess_id or "")
                status = "succeeded"
                try:
                    async for ev in original(self, envelope):
                        # A terminal error frame (e.g. agent loop hit MAX_STEPS ->
                        # yields e2a.error) is a normal yield+return, NOT an
                        # exception — without this check the span would be
                        # mislabeled "succeeded" even though the task failed.
                        if status != "failed":
                            rk = getattr(ev, "response_kind", None)
                            est = getattr(ev, "status", None)
                            if rk == "e2a.error" or est == "failed":
                                status = "failed"
                                span.set_status(Status(StatusCode.ERROR))
                        yield ev
                except Exception as exc:
                    status = "failed"
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                finally:
                    try:
                        span.set_attribute(A.TWINKLE_AGENT_STATUS, status)
                        span.set_attribute(A.TWINKLE_AGENT_ITERATIONS, current_llm_counter())
                        metrics.record_agent_duration(status, time.perf_counter() - start)
                    except Exception:
                        pass
                    ctr_tok.reset()
                    rctx_tok.reset()

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(agent_cls, "run_stream", factory)
