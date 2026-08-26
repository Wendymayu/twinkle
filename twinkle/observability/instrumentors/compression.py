"""插桩 compression.do_compress -> twinkle.compression span。

patch ``compression`` 模块上的 ``do_compress``（而非 ``compress_messages``）。
``do_compress`` 仅在 ``should_compress`` 为 True 时调用，故 wrapper 总是
开 span，无假阳性、无预测逻辑。因 ``do_compress`` 是 ``compress_messages``
的同模块被调者（调用时经 module globals 解析，非 import 绑定），patch 它
可零 hook 改动地触达两处生产调用点（ContextCompressionHook +
ContextOverflowRecoveryHook）。``_summarize`` 内的 ``llm.stream`` 发出的
``gen_ai.chat`` span 嵌套在本 span 下（经 ``start_as_current_span`` 成为
current）。
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx


def _has_summary(msgs: list[dict]) -> bool:
    """返回的 messages 含 ``[prior context summary]`` system msg 时为 True。

    区分正常 summary 路径与 ``_summarize`` 失败的降级路径
    （head + tail、middle 丢弃、无 summary message）。
    """
    for m in msgs:
        if (m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[prior context summary]")):
            return True
    return False


def instrument_compression(tracer, metrics, cfg, *, compression_mod=None) -> bool:
    """Patch ``compression.do_compress`` 以发出 ``twinkle.compression`` span。

    ``metrics`` 为与兄弟 instrumentor 签名对齐而保留，但未使用
    （compression 低频；span 足矣）。
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
