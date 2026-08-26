"""twinkle.observability —— agentserver 可观测性（OTel + monkey-patch）。

setup() 是唯一入口：读 config，若 OTEL_ENABLED，则初始化 OTel providers
并 monkey-patch 3 个 agentserver 咽喉点（ReActAgent.run / LLMClient.stream /
ToolManager.execute）。幂等 + fail-soft；OTEL_ENABLED=false（默认）是零开销
no-op。
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

        # Metrics(None) 是静默 no-op，故 traces 开 + metrics 关不会崩。
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
