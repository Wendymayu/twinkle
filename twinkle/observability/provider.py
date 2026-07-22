"""init_providers — build TracerProvider + MeterProvider; OTLP gRPC/console/none.

Returns (tracer, meter); does NOT set global providers — instrumentors take
the tracer/meter as params, so tests stay free of global-provider pollution.
Fail-soft: any error -> log + that signal disabled.
"""
from __future__ import annotations

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
        return mp.get_meter("twinkle")
    except Exception:
        log.exception("meter provider init failed; metrics disabled")
        return None
