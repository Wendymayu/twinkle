"""twinkle.observability — agentserver observability (OTel + monkey-patch).

setup() is the single entry point: reads config, and if OTEL_ENABLED,
initializes OTel providers and monkey-patches the 3 agentserver choke
points (AgentLoop.run_stream / LLMClient.stream / ToolManager.execute).
Idempotent + fail-soft; OTEL_ENABLED=false (default) is a zero-cost no-op.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability")

_APPLIED = False


def setup() -> bool:
    global _APPLIED
    if _APPLIED:
        return True
    try:
        from twinkle.observability.config import load_config

        cfg = load_config()
        if not cfg.enabled:
            return False
        try:
            from twinkle.observability.provider import init_providers
        except ImportError:
            log.warning(
                "opentelemetry not installed; observability disabled (pip install -e '.[obs]')"
            )
            return False
        tracer, meter = init_providers(cfg)
        if tracer is None:
            log.warning("observability enabled but traces disabled; instrumentation needs a tracer")
            return False
        from twinkle.observability.instrumentors import apply_instrumentors
        from twinkle.observability.metrics import Metrics

        # Metrics(None) is a silent no-op, so traces-on + metrics-off won't crash.
        metrics = Metrics(meter)
        apply_instrumentors(tracer, metrics, cfg)
        _APPLIED = True
        log.info(
            "twinkle observability applied (traces=%s metrics=%s)", True, meter is not None
        )
        return True
    except Exception:
        log.exception("twinkle observability setup failed; continuing without telemetry")
        return False
