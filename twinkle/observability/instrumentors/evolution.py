"""Instrument OnlineEvolutionOrchestrator.evolve -> twinkle.skill.evolution span.

Patches ``evolve`` (per-skill, contains the signal-detection + experience-
generation LLM calls, returns an ``EvolutionResult`` with ``.status``). The
internal LLM calls' ``gen_ai.chat`` spans nest under this span (current via
``start_as_current_span``). ``run_feedback_loop`` is NOT patched — it returns
None (no status), so a span there has low diagnostic value; its LLM calls stay
indistinguishable ``gen_ai.chat`` (accepted, YAGNI).
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx, _trunc


def instrument_evolution(tracer, metrics, cfg, *, orchestrator_cls=None) -> bool:
    """Patch ``OnlineEvolutionOrchestrator.evolve`` to emit a
    ``twinkle.skill.evolution`` span carrying ``skill.name`` / ``evolution.status``
    / ``evolution.message``.

    ``metrics`` is accepted for signature parity but unused (evolution is
    low-frequency; spans suffice).
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
