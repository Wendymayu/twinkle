"""init_providers — build TracerProvider + MeterProvider; OTLP gRPC/console/none.

Returns (tracer, meter); also sets global providers so that:
1. BatchSpanProcessor gets flushed on shutdown
2. Third-party OTel instrumentation (e.g. grpc/aiohttp) picks up the same provider
Fail-soft: any error -> log + that signal disabled.
"""
from __future__ import annotations

import atexit
import logging

log = logging.getLogger("twinkle.observability.provider")


def _is_insecure(endpoint: str) -> bool:
    # http:// -> plaintext gRPC (insecure=True); https:// -> TLS.
    return endpoint.lower().startswith("http://")


def init_providers(cfg):
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": cfg.service_name})
    tracer = _init_tracer(cfg, resource)
    meter = _init_meter(cfg, resource)
    return tracer, meter


def _init_tracer(cfg, resource):
    if cfg.traces_exporter == "none":
        return None
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        tp = TracerProvider(resource=resource)
        if cfg.traces_exporter == "console":
            tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif cfg.traces_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            tp.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=cfg.endpoint,
                        headers=cfg.headers or None,
                        insecure=_is_insecure(cfg.endpoint),
                    )
                )
            )

        # Set global provider so BatchSpanProcessor flushes on shutdown
        from opentelemetry import trace
        trace.set_tracer_provider(tp)

        # Register atexit to flush pending spans
        def _shutdown_tracer():
            try:
                tp.shutdown()
            except Exception:
                pass
        atexit.register(_shutdown_tracer)

        return tp.get_tracer("twinkle")
    except Exception:
        log.exception("tracer provider init failed; traces disabled")
        return None


def _init_meter(cfg, resource):
    if cfg.metrics_exporter == "none":
        return None
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        readers = []
        if cfg.metrics_exporter == "console":
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

            readers.append(
                PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=3000)
            )
        elif cfg.metrics_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=cfg.endpoint,
                        headers=cfg.headers or None,
                        insecure=_is_insecure(cfg.endpoint),
                    ),
                    export_interval_millis=3000,
                )
            )
        mp = MeterProvider(metric_readers=readers, resource=resource)

        # Set global provider so PeriodicExportingMetricReader flushes on shutdown
        from opentelemetry import metrics
        metrics.set_meter_provider(mp)

        # Register atexit to flush pending metrics
        def _shutdown_meter():
            try:
                mp.shutdown()
            except Exception:
                pass
        atexit.register(_shutdown_meter)

        return mp.get_meter("twinkle")
    except Exception:
        log.exception("meter provider init failed; metrics disabled")
        return None
