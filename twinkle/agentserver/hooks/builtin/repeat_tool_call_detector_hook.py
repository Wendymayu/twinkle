"""RepeatToolCallDetectorHook — 滑动窗口 + 稳定哈希检测重复 tool call。

4 档严重度（LOW→CRITICAL），边沿触发，检测到循环时自动注入纠偏 system
消息。限流：每 session 每分钟最多 N 次注入。
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
    """4 档严重度 — rank 高者更严重。"""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class _SessionState:
    """检测器的 per-session 运行时状态。"""
    history: deque = field(default_factory=lambda: deque(maxlen=30))
    pending_call_key: str | None = None
    fired_severity: Severity | None = None
    remediation_timestamps: list[float] = field(default_factory=list)


def stable_call_hash(name: str, args: dict) -> str:
    """tool name + 排序后 args 的稳定哈希 — 参数顺序不影响检测。"""
    payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def stable_result_hash(result: str) -> str:
    """tool result 的稳定哈希 — 区分"重复调用但结果在变"与"结果相同"。"""
    return hashlib.sha256(result.encode()).hexdigest()[:16]


class RepeatToolCallDetectorHook(AgentHook):
    """重复/循环 tool-call 检测器 + 自动纠偏。

    Priority 88：在 SkillHook(90) 之后、MemoryHook(80) 之前。
    需在压缩之后看到 messages，但在 memory 注入之前注入纠偏。

    事件：
    - before_tool_call：记录 call_key
    - after_tool_call：记录 outcome_key + 分类检测
    - on_tool_exception：把错误记为 outcome_key + 分类检测
    - before_model_call：检测到循环则注入纠偏 system message
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

        # per-session 运行时状态 — 以 session_id 为键
        self._states: dict[str, _SessionState] = {}

    def _get_state(self, ctx: HookContext) -> _SessionState:
        """获取或创建 per-session 状态。"""
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
        """CRITICAL 循环硬停；MEDIUM/HIGH 注入纠偏提示。

        CRITICAL（尾部相同 call+outcome >= global_stop）意味着
        agent 毫无进展 — 用 force_finish 结束本次 run，而非提示。
        此处绕过纠偏限流器：卡死的循环必须停，而不是被警告。
        MEDIUM/HIGH 仍是软纠偏（模型仍有机会换策略）。
        """
        state = self._get_state(ctx)
        if state.fired_severity is None or state.fired_severity < Severity.MEDIUM:
            return
        # CRITICAL：硬停循环。
        if state.fired_severity >= Severity.CRITICAL:
            ctx.request_force_finish(
                result=(
                    "agent stopped: repeated tool-call loop detected (CRITICAL) "
                    "— the agent was making no progress. Please rephrase the task or add detail."
                )
            )
            log.warning(
                "[RepeatToolDetection] CRITICAL loop -> force_finish (hard stop), call_key=%s",
                (state.history[-1][0][:8] if state.history else "?"),
            )
            return
        # MEDIUM/HIGH：软纠偏提示（限流）。
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

    # --- 内部方法 ---

    def _record_and_classify(self, ctx: HookContext, outcome: str) -> None:
        """记录已完成的 call 并运行分类检测。"""
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
        # 边沿触发：仅当严重度上升时触发
        if state.fired_severity is not None and severity <= state.fired_severity:
            return
        state.fired_severity = severity
        log.warning(
            "[RepeatToolDetection] Anomaly detected: severity=%s, call_key=%s",
            severity.name, call_key[:8],
        )

    def _classify(self, state: _SessionState, call_key: str) -> Severity | None:
        """4 档分类检测，返回最高严重度。"""
        repeat_warn = self._repeat_warn or _get_repeat_warn()
        pingpong_warn = self._pingpong_warn or _get_pingpong_warn()
        loop_block = self._loop_block or _get_loop_block()
        global_stop = self._global_stop or _get_global_stop()

        # CRITICAL / HIGH：尾部相同（call+outcome）
        trailing = self._trailing_identical(state)
        if trailing >= global_stop:
            return Severity.CRITICAL
        if trailing >= loop_block:
            return Severity.HIGH

        # MEDIUM：A-B-A-B 交替
        alternation = self._trailing_alternation(state)
        if alternation >= pingpong_warn:
            return Severity.MEDIUM

        # LOW：窗口内同一 call_key 重复
        repeats = sum(1 for history_key, _ in state.history if history_key == call_key)
        if repeats >= repeat_warn:
            return Severity.LOW

        return None

    def _trailing_identical(self, state: _SessionState) -> int:
        """尾部连续相同 (call_key, outcome_key) 对的数量。"""
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
        """尾部 A-B-A-B 交替模式的数量。"""
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
        """限流：每分钟最多 N 次注入。"""
        max_per_minute = self._remediation_max_per_minute or _get_remediation_max_per_minute()
        now = time.monotonic()
        state.remediation_timestamps = [
            ts for ts in state.remediation_timestamps if now - ts < 60
        ]
        return len(state.remediation_timestamps) < max_per_minute


# --- config 懒读 ---

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
