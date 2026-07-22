"""ObservabilityConfig — env-driven, default-off.

Mirrors jiuwenswarm-instrumentor config.py. OTEL_ENABLED=false (default)
=> setup() is a zero-cost no-op. Importing twinkle.config triggers the
repo-root .env loader (side effect) so os.getenv sees .env values too.
"""
from __future__ import annotations

import os

import twinkle.config  # noqa: F401 — triggers .env loading


def _get_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _get_headers(key: str) -> dict[str, str]:
    raw = os.getenv(key, "")
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
    return out


class ObservabilityConfig:
    def __init__(self) -> None:
        self.enabled = _get_bool("OTEL_ENABLED", False)
        self.traces_exporter = os.getenv("OTEL_TRACES_EXPORTER", "none").lower()
        self.metrics_exporter = os.getenv("OTEL_METRICS_EXPORTER", "none").lower()
        self.protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()
        self.endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self.headers = _get_headers("OTEL_EXPORTER_OTLP_HEADERS")
        self.service_name = os.getenv("OTEL_SERVICE_NAME", "twinkle-agentserver")


def load_config() -> ObservabilityConfig:
    return ObservabilityConfig()
