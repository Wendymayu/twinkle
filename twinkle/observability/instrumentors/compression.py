"""Instrument compression.do_compress -> twinkle.compression span.

Patches ``do_compress`` (not ``compress_messages``) on the ``compression``
module. ``do_compress`` is only called when ``should_compress`` is True, so the
wrapper always opens a span with no false positives and no prediction logic.
Because ``do_compress`` is a same-module callee of ``compress_messages``
(resolved via module globals at call time, not import-bound), patching it
reaches both production call sites (ContextCompressionHook +
ContextOverflowRecoveryHook) with zero hook changes. The summary ``llm.stream``
inside ``_summarize`` emits a ``gen_ai.chat`` span that nests under this span
(span is current via ``start_as_current_span``).
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx


def _has_summary(msgs: list[dict]) -> bool:
    """True if the returned messages contain a ``[prior context summary]`` system msg.

    Distinguishes the normal summary path from the ``_summarize``-failed degrade
    path (head + tail, middle dropped, no summary message).
    """
    for m in msgs:
        if (m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[prior context summary]")):
            return True
    return False


def instrument_compression(tracer, metrics, cfg, *, compression_mod=None) -> bool:
    """Patch ``compression.do_compress`` to emit a ``twinkle.compression`` span.

    ``metrics`` is accepted for signature parity with sibling instrumentors but
    unused (compression is low-frequency; spans suffice).
    """
    if compression_mod is None:
        from twinkle.agentserver import compression as compression_mod

    estimate_tokens = compression_mod.estimate_tokens

    def factory(original):
        async def traced(msgs, llm, *, keep_recent_pairs, summary_system_prompt):
            before_tokens = estimate_tokens(msgs)
            with tracer.start_as_current_span(A.SPAN_COMPRESSION) as span:
                _stamp_ctx(span)
                span.set_attribute(A.TWINKLE_COMPRESSION_TOKENS_BEFORE, before_tokens)
                try:
                    result = await original(
                        msgs, llm,
                        keep_recent_pairs=keep_recent_pairs,
                        summary_system_prompt=summary_system_prompt,
                    )
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                after_tokens = estimate_tokens(result)
                span.set_attribute(A.TWINKLE_COMPRESSION_TOKENS_AFTER, after_tokens)
                span.set_attribute(A.TWINKLE_COMPRESSION_COMPRESSED,
                                   after_tokens < before_tokens)
                span.set_attribute(A.TWINKLE_COMPRESSION_HAS_SUMMARY,
                                   _has_summary(result))
                span.set_attribute(A.TWINKLE_COMPRESSION_STRATEGY, "inline_summary")
                return result
        return traced

    from twinkle.observability.wrap import patch_method
    return patch_method(compression_mod, "do_compress", factory)
