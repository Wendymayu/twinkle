"""Instrument ToolManager.execute -> gen_ai.tool span。"""
from __future__ import annotations

import json
import time

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx, _trunc


def instrument_tool(tracer, metrics, cfg, *, tool_cls=None) -> bool:
    if tool_cls is None:
        from twinkle.agentserver.tools.manager import ToolManager as tool_cls

    def factory(original):
        async def traced(self, name, args):
            start = time.perf_counter()
            error = False
            # start_as_current_span（而非 start_span）使 tool span 在
            # `original` 期间成为 current span，故 tool 内部创建的 span——
            # 如 spawn_subagent 里 subagent 的 twinkle.agent.invoke——
            # 挂在本 tool span 下，而非外层 agent invoke 下。
            with tracer.start_as_current_span(A.SPAN_GEN_AI_TOOL) as span:
                _stamp_ctx(span)
                span.set_attribute(A.GEN_AI_TOOL_NAME, name or "")
                try:
                    span.set_attribute(A.GEN_AI_TOOL_ARGUMENTS, _trunc(json.dumps(args, ensure_ascii=False)))
                except Exception:
                    pass
                try:
                    result = await original(self, name, args)
                    if isinstance(result, str) and result.startswith(A.TOOL_ERROR_PREFIX):
                        error = True
                    span.set_attribute(A.GEN_AI_TOOL_ERROR, error)
                    try:
                        span.set_attribute(A.GEN_AI_TOOL_RESULT, _trunc(str(result)))
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    span.set_attribute(A.GEN_AI_TOOL_ERROR, True)
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                finally:
                    metrics.record_tool_call(name, error, time.perf_counter() - start)
                    # span.end() 由 `with start_as_current_span` context manager 处理。

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(tool_cls, "execute", factory)
