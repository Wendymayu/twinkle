"""Instrument MemoryFlushHook._flush -> spans.

instrument_memory_flush: patches ``_flush`` (the 兜底 work method, only called
past the config gate + ``should_compress`` True + non-empty ``middle`` — so the
span always reflects a real fire, no false positives, no prediction logic —
mirroring how the compression instrumentor patches ``do_compress`` not
``compress_messages``). The internal ``llm.stream`` call's ``gen_ai.chat`` span
nests under this span. The hook itself stays free of any OTel code.
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx


def instrument_memory_flush(tracer, metrics, cfg, *, hook_cls=None) -> bool:
    """Patch ``MemoryFlushHook._flush`` to emit a ``twinkle.memory.flush`` span
    carrying ``flush.new_writes`` / ``flush.errors``.

    ``metrics`` accepted for signature parity with sibling instrumentors but
    unused (flush is low-frequency; spans suffice).
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
