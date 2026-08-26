"""Instrument MemoryFlushHook._flush -> span。

instrument_memory_flush：patch ``_flush``（兜底工作方法，仅在过 config 门 +
``should_compress`` 为 True + ``middle`` 非空时调用——故 span 总反映真实
触发，无假阳性、无预测逻辑——镜像 compression instrumentor patch
``do_compress`` 而非 ``compress_messages`` 的做法）。内部 ``llm.stream`` 调用
的 ``gen_ai.chat`` span 嵌套在本 span 下。hook 本身不含任何 OTel 代码。
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx


def instrument_memory_flush(tracer, metrics, cfg, *, hook_cls=None) -> bool:
    """Patch ``MemoryFlushHook._flush`` 以发出 ``twinkle.memory.flush`` span，
    携带 ``flush.new_writes`` / ``flush.errors``。

    ``metrics`` 为与兄弟 instrumentor 签名对齐而保留，但未使用
    （flush 低频；span 足矣）。
    """
    if hook_cls is None:
        from twinkle.agentserver.hooks.builtin.memory_flush_hook import (
            MemoryFlushHook as hook_cls,
        )

    def factory(original):
        async def traced(self, middle):
            with tracer.start_as_current_span(A.SPAN_MEMORY_FLUSH) as span:
                _stamp_ctx(span)
                try:
                    new_writes, errors = await original(self, middle)
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                span.set_attribute(A.TWINKLE_MEMORY_FLUSH_NEW_WRITES, new_writes)
                span.set_attribute(A.TWINKLE_MEMORY_FLUSH_ERRORS, errors)
                return new_writes, errors
        return traced

    from twinkle.observability.wrap import patch_method
    return patch_method(hook_cls, "_flush", factory)
