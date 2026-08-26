"""Instrument OnlineEvolutionOrchestrator.evolve -> twinkle.skill.evolution span。

patch ``evolve``（按 skill，含 signal-detection + experience-generation 的
LLM 调用，返回带 ``.status`` 的 ``EvolutionResult``）。内部 LLM 调用的
``gen_ai.chat`` span 嵌套在本 span 下（经 ``start_as_current_span`` 成为
current）。``run_feedback_loop`` 不 patch——它返回 None（无 status），
故该处 span 诊断价值低；其 LLM 调用仍是不可区分的 ``gen_ai.chat``
（接受，YAGNI）。
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx, _trunc


def instrument_evolution(tracer, metrics, cfg, *, orchestrator_cls=None) -> bool:
    """Patch ``OnlineEvolutionOrchestrator.evolve`` 以发出
    ``twinkle.skill.evolution`` span，携带 ``skill.name`` / ``evolution.status``
    / ``evolution.message``。

    ``metrics`` 为签名对齐而保留，但未使用（evolution 低频；span 足矣）。
    """
    if orchestrator_cls is None:
        from twinkle.agentserver.evolution.orchestrator import (
            OnlineEvolutionOrchestrator as orchestrator_cls,
        )

    def factory(original):
        async def traced(self, skill_name, conversation_messages, *args, **kwargs):
            with tracer.start_as_current_span(A.SPAN_SKILL_EVOLUTION) as span:
                _stamp_ctx(span)
                span.set_attribute(A.TWINKLE_SKILL_NAME, skill_name or "")
                try:
                    result = await original(
                        self, skill_name, conversation_messages, *args, **kwargs
                    )
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                if result is not None:
                    span.set_attribute(A.TWINKLE_EVOLUTION_STATUS,
                                       getattr(result, "status", "") or "")
                    span.set_attribute(A.TWINKLE_EVOLUTION_MESSAGE,
                                       _trunc(getattr(result, "message", "") or ""))
                return result
        return traced

    from twinkle.observability.wrap import patch_method
    return patch_method(orchestrator_cls, "evolve", factory)
