"""Instrument ReActAgent.run -> twinkle.agent.invoke（root span）。

以 current 打开 root span，使 child gen_ai.chat / gen_ai.tool span
（parent = current）挂在其下。把 request_id/session_id 盖到 ContextVar，
使 child span 能经 _stamp_ctx 取到。经 _llm_call_counter 计 LLM 调用，
以便在 span 结束时设 twinkle.agent.iterations。
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
        from twinkle.agentserver.agent import ReActAgent as agent_cls

    def factory(original):
        async def traced(self, request):
            req_id = getattr(request, "request_id", None)
            sess_id = getattr(request, "session_id", None)
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
                    async for ev in original(self, request):
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

    return patch_method(agent_cls, "run", factory)
