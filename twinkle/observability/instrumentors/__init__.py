"""Apply all agentserver instrumentors.

Each instrumentor is applied in its own try/except so one failing surface
doesn't break the rest. Production passes *_cls=None (lazy import of the
real class); tests pass fakes.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability")


def apply_instrumentors(tracer, metrics, cfg, *, agent_cls=None, llm_cls=None, tool_cls=None):
    from twinkle.observability.instrumentors.agent import instrument_agent
    from twinkle.observability.instrumentors.llm import instrument_llm
    from twinkle.observability.instrumentors.tool import instrument_tool

    results = {}
    for label, fn in (
        ("agent", lambda: instrument_agent(tracer, metrics, cfg, agent_cls=agent_cls)),
        ("llm", lambda: instrument_llm(tracer, metrics, cfg, llm_cls=llm_cls)),
        ("tool", lambda: instrument_tool(tracer, metrics, cfg, tool_cls=tool_cls)),
    ):
        try:
            results[label] = fn()
        except Exception:
            log.exception("instrumentor %s failed", label)
            results[label] = False
    return results
