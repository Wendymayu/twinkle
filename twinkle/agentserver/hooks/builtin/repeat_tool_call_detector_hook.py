"""RepeatToolCallDetectorHook — sliding window + stable hash detection of repeated tool calls.

4-tier severity (LOW→CRITICAL), edge-triggered, auto-injects remediation system
messages on loops. Rate-limited: at most N injections per minute per session.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from enum import IntEnum

from dataclasses import dataclass, field

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.repeat_tool_detection")


class Severity(IntEnum):
    """4-tier severity — higher rank means more severe."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class _SessionState:
    """Per-session runtime state for the detector."""
    history: deque = field(default_factory=lambda: deque(maxlen=30))
    pending_call_key: str | None = None
    fired_severity: Severity | None = None
    remediation_timestamps: list[float] = field(default_factory=list)


def stable_call_hash(name: str, args: dict) -> str:
    """Stable hash of tool name + sorted args — parameter order does not affect detection."""
    payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def stable_result_hash(result: str) -> str:
    """Stable hash of tool result — distinguishes 'repeated calls with changing results' vs 'same results'."""
    return hashlib.sha256(result.encode()).hexdigest()[:16]


class RepeatToolCallDetectorHook(AgentHook):
    """Repeat / loop tool-call detector + auto-remediator.

    Priority 88: after SkillHook(90), before MemoryHook(80).
    Needs to see messages after compression, but inject remediation before memory injection.

    Events:
    - before_tool_call: record call_key
    - after_tool_call: record outcome_key + classify detection
    - on_tool_exception: record error as outcome_key + classify detection
    - before_model_call: inject remediation system message if loop detected
    """

    priority = 88

    def __init__(
        self,
        *,
        history_size: int | None = None,
        repeat_warn: int | None = None,
        pingpong_warn: int | None = None,
        loop_block: int | None = None,
        global_stop: int | None = None,
        remediation_max_per_minute: int | None = None,
    ) -> None:
        self._history_size = history_size
        self._repeat_warn = repeat_warn
        self._pingpong_warn = pingpong_warn
        self._loop_block = loop_block
        self._global_stop = global_stop
        self._remediation_max_per_minute = remediation_max_per_minute

        # Per-session runtime state — keyed by session_id
        self._states: dict[str, _SessionState] = {}

    def _get_state(self, ctx: HookContext) -> _SessionState:
        """Get or create per-session state."""
        session_id = ctx.session_id or "_default"
        if session_id not in self._states:
            self._states[session_id] = _SessionState(
                history=deque(maxlen=self._history_size or _get_history_size()),
                pending_call_key=None,
                fired_severity=None,
                remediation_timestamps=[],
            )
        return self._states[session_id]

    async def before_tool_call(self, ctx: HookContext) -> None:
        state = self._get_state(ctx)
        state.pending_call_key = stable_call_hash(
            ctx.inputs.name, ctx.inputs.args  # type: ignore[attr-defined]
        )

    async def after_tool_call(self, ctx: HookContext) -> None:
        result = ctx.extra.get("_tool_result", "")
        self._record_and_classify(ctx, result)

    async def on_tool_exception(self, ctx: HookContext) -> None:
        outcome = str(ctx.exception) if ctx.exception else "error"
        self._record_and_classify(ctx, outcome)

    async def before_model_call(self, ctx: HookContext) -> None:
        """Inject remediation message in before_model_call if loop detected."""
        state = self._get_state(ctx)
        if state.fired_severity is None or state.fired_severity < Severity.MEDIUM:
            return
        if not self._check_remediation_budget(state):
            return
        severity_label = state.fired_severity.name
        ctx.inputs.messages = list(ctx.inputs.messages) + [{
            "role": "system",
            "content": (
                f"[DETECTION] Repeated tool call pattern detected (severity: {severity_label}). "
                "Please try a different strategy, use different parameters, or confirm requirements with the user. "
                "Do not repeat the same tool call."
            ),
        }]
        state.remediation_timestamps.append(time.monotonic())
        log.info(
            "[RepeatToolDetection] Injected remediation message (severity=%s)",
            severity_label,
        )

    # --- Internal methods ---

    def _record_and_classify(self, ctx: HookContext, outcome: str) -> None:
        """Record completed call and run classification detection."""
        state = self._get_state(ctx)
        if state.pending_call_key is None:
            return
        call_key = state.pending_call_key
        state.pending_call_key = None
        outcome_key = stable_result_hash(outcome[:1000])
        state.history.append((call_key, outcome_key))

        severity = self._classify(state, call_key)
        if severity is None:
            return
        # Edge-triggered: only fire when severity rises
        if state.fired_severity is not None and severity <= state.fired_severity:
            return
        state.fired_severity = severity
        log.warning(
            "[RepeatToolDetection] Anomaly detected: severity=%s, call_key=%s",
            severity.name, call_key[:8],
        )

    def _classify(self, state: _SessionState, call_key: str) -> Severity | None:
        """4-tier classification detection, returns highest severity."""
        repeat_warn = self._repeat_warn or _get_repeat_warn()
        pingpong_warn = self._pingpong_warn or _get_pingpong_warn()
        loop_block = self._loop_block or _get_loop_block()
        global_stop = self._global_stop or _get_global_stop()

        # CRITICAL / HIGH: trailing identical (call+outcome)
        trailing = self._trailing_identical(state)
        if trailing >= global_stop:
            return Severity.CRITICAL
        if trailing >= loop_block:
            return Severity.HIGH

        # MEDIUM: A-B-A-B alternation
        alternation = self._trailing_alternation(state)
        if alternation >= pingpong_warn:
            return Severity.MEDIUM

        # LOW: same call_key repeated in window
        repeats = sum(1 for history_key, _ in state.history if history_key == call_key)
        if repeats >= repeat_warn:
            return Severity.LOW

        return None

    def _trailing_identical(self, state: _SessionState) -> int:
        """Count of trailing consecutive identical (call_key, outcome_key) pairs."""
        if not state.history:
            return 0
        last = state.history[-1]
        count = 0
        for record in reversed(state.history):
            if record == last:
                count += 1
            else:
                break
        return count

    def _trailing_alternation(self, state: _SessionState) -> int:
        """Count of trailing A-B-A-B alternation pattern."""
        if len(state.history) < 2:
            return 0
        sequence = list(reversed(state.history))
        first = sequence[0]
        second = sequence[1]
        if first == second or first[0] == second[0]:
            return 0
        count = 0
        for idx, record in enumerate(sequence):
            expected = first if idx % 2 == 0 else second
            if record == expected:
                count += 1
            else:
                break
        return count

    def _check_remediation_budget(self, state: _SessionState) -> bool:
        """Rate-limit: at most N injections per minute."""
        max_per_minute = self._remediation_max_per_minute or _get_remediation_max_per_minute()
        now = time.monotonic()
        state.remediation_timestamps = [
            ts for ts in state.remediation_timestamps if now - ts < 60
        ]
        return len(state.remediation_timestamps) < max_per_minute


# --- Config lazy reads ---

def _get_history_size() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.history_size


def _get_repeat_warn() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.repeat_warn


def _get_pingpong_warn() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.pingpong_warn


def _get_loop_block() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.loop_block


def _get_global_stop() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.global_stop


def _get_remediation_max_per_minute() -> int:
    from twinkle.config import settings
    return settings.repeat_tool_detection.remediation_max_per_minute
