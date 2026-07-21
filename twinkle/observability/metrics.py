"""Metrics — fail-soft wrappers over OTel counters/histograms."""
from __future__ import annotations

import logging

from twinkle.observability import attributes as A
from twinkle.observability.usage import read_usage_token

log = logging.getLogger("twinkle.observability.metrics")


class Metrics:
    def __init__(self, meter) -> None:
        self._meter = meter
        if meter is None:
            # No meter (metrics disabled) -> all instruments no-op silently;
            # avoids noisy tracebacks when traces are on but metrics are off.
            self._token_usage = self._tool_count = None
            self._llm_duration = self._tool_duration = self._agent_duration = None
            return
        self._token_usage = self._create_counter(A.METRIC_TOKEN_USAGE, "LLM token usage", "token")
        self._tool_count = self._create_counter(A.METRIC_TOOL_COUNT, "Tool invocations", "1")
        self._llm_duration = self._create_histogram(A.METRIC_LLM_DURATION, "LLM call duration", "s")
        self._tool_duration = self._create_histogram(A.METRIC_TOOL_DURATION, "Tool call duration", "s")
        self._agent_duration = self._create_histogram(A.METRIC_AGENT_DURATION, "Agent invoke duration", "s")

    def _create_counter(self, name, desc, unit):
        try:
            return self._meter.create_counter(name, unit=unit, description=desc)
        except Exception:
            log.exception("create_counter failed: %s", name)
            return None

    def _create_histogram(self, name, desc, unit):
        try:
            return self._meter.create_histogram(name, unit=unit, description=desc)
        except Exception:
            log.exception("create_histogram failed: %s", name)
            return None

    def record_token_usage(self, usage, model: str) -> None:
        if not usage or not self._token_usage:
            return
        try:
            attrs = {A.GEN_AI_REQUEST_MODEL: model or "unknown"}
            # usage may be a dict (fakes/tests) or a pydantic object (real
            # openai SDK CompletionUsage); read_usage_token handles both.
            inp = read_usage_token(usage, "prompt_tokens", "input_tokens")
            out = read_usage_token(usage, "completion_tokens", "output_tokens")
            tot = read_usage_token(usage, "total_tokens")
            if inp is not None:
                self._token_usage.add(int(inp), {**attrs, A.GEN_AI_TOKEN_TYPE: "input"})
            if out is not None:
                self._token_usage.add(int(out), {**attrs, A.GEN_AI_TOKEN_TYPE: "output"})
            if tot is not None and inp is None and out is None:
                self._token_usage.add(int(tot), {**attrs, A.GEN_AI_TOKEN_TYPE: "total"})
        except Exception:
            log.exception("record_token_usage failed")

    def record_tool_call(self, name: str, error: bool, duration_s: float) -> None:
        if not self._tool_count or not self._tool_duration:
            return
        try:
            attrs = {A.GEN_AI_TOOL_NAME: name or "unknown", A.GEN_AI_TOOL_ERROR: error}
            self._tool_count.add(1, attrs)
            self._tool_duration.record(duration_s, {A.GEN_AI_TOOL_NAME: name or "unknown"})
        except Exception:
            log.exception("record_tool_call failed")

    def record_llm_duration(self, model: str, duration_s: float) -> None:
        if not self._llm_duration:
            return
        try:
            self._llm_duration.record(duration_s, {A.GEN_AI_REQUEST_MODEL: model or "unknown"})
        except Exception:
            log.exception("record_llm_duration failed")

    def record_agent_duration(self, status: str, duration_s: float) -> None:
        if not self._agent_duration:
            return
        try:
            self._agent_duration.record(duration_s, {A.TWINKLE_AGENT_STATUS: status})
        except Exception:
            log.exception("record_agent_duration failed")
