"""Apply all agentserver instrumentors.

Each instrumentor is applied in its own try/except so one failing surface
doesn't break the rest. Production passes *_cls=None (lazy import of the
real class); tests pass fakes.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability")


def apply_instrumentors(tracer, metrics, cfg, *, agent_cls=None, llm_cls=None,
                        tool_cls=None, compression_mod=None,
                        orchestrator_cls=None, hook_cls=None):
    from twinkle.observability.instrumentors.agent import instrument_agent
    from twinkle.observability.instrumentors.llm import instrument_llm
    from twinkle.observability.instrumentors.tool import instrument_tool
    from twinkle.observability.instrumentors.compression import instrument_compression
    from twinkle.observability.instrumentors.evolution import instrument_evolution
    from twinkle.observability.instrumentors.memory import instrument_memory_flush

    results = {}
    for label, fn in (
        ("agent", lambda: instrument_agent(tracer, metrics, cfg, agent_cls=agent_cls)),
        ("llm", lambda: instrument_llm(tracer, metrics, cfg, llm_cls=llm_cls)),
        ("tool", lambda: instrument_tool(tracer, metrics, cfg, tool_cls=tool_cls)),
        ("compression", lambda: instrument_compression(tracer, metrics, cfg, compression_mod=compression_mod)),
        ("evolution", lambda: instrument_evolution(tracer, metrics, cfg, orchestrator_cls=orchestrator_cls)),
        ("memory_flush", lambda: instrument_memory_flush(tracer, metrics, cfg, hook_cls=hook_cls)),
    ):
        try:
            results[label] = fn()
        except Exception:
            log.exception("instrumentor %s failed", label)
            results[label] = False
    return results
