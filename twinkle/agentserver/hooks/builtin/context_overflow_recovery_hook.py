"""ContextOverflowRecoveryHook — on_model_exception 溢出恢复。

被动恢复：LLM 抛 413 / context_length_exceeded 时，强制更激进压缩后重试。
连续失败熔断：超过 max_recovery_attempts 后注入熔断消息让 LLM 产出最终回答。
成功后重置计数器。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from twinkle.agentserver.compression import compress_messages, estimate_tokens
from twinkle.agentserver.hooks.base import AgentHook, HookContext

if TYPE_CHECKING:
    from twinkle.agentserver.llm_client import LLMClient

log = logging.getLogger("twinkle.hooks.overflow_recovery")


def _parse_token_limits(exc: Exception) -> tuple[int | None, int | None]:
    """从 413 错误解析 actual_tokens 和 limit_tokens。

    Returns: (actual_tokens, limit_tokens) — None 表示未解析到。
    """
    msg = str(exc)

    # Anthropic: "prompt is too long: N tokens > M"
    m = re.search(r'(\d+)\s*tokens?\s*>\s*(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # OpenAI: "maximum context length is N"
    m = re.search(r'maximum context length is\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return None, int(m.group(1))

    return None, None


_OVERFLOW_KEYWORDS = (
    "prompt is too long",       # Anthropic
    "input too long",           # Anthropic 新格式
    "context_length_exceeded",  # OpenAI 标准 error code
    "maximum context length",   # OpenAI
    "context length exceeded",  # 通用
)


def _is_context_overflow_error(exc: Exception) -> bool:
    """判断异常是否为上下文溢出错误。

    3 层判定：
    1. status_code==413 → 直接判定
    2. status_code==400 + 溢出关键词 → 判定
    3. status_code is None + 溢出关键词 → 兜底判定
    """
    status_code = getattr(exc, "status_code", None)
    msg_lower = str(exc).lower()
    has_keyword = any(kw in msg_lower for kw in _OVERFLOW_KEYWORDS)

    if status_code == 413:
        return True
    if status_code == 400 and has_keyword:
        return True
    if status_code is None and has_keyword:
        return True
    return False


class ContextOverflowRecoveryHook(AgentHook):
    """Context overflow recovery — reactive 413 handling.

    Priority 60: 在 RetryHook(50) 之前，先处理溢出恢复。
    RetryHook 不处理 413（不属于 transient），所以两者不冲突。
    """

    priority = 60

    def __init__(
        self,
        llm: "LLMClient",
        *,
        max_recovery_attempts: int | None = None,
        aggressive_keep_recent: int | None = None,
        threshold_ratio: float | None = None,
    ) -> None:
        self._llm = llm
        self._max_recovery_attempts = max_recovery_attempts
        self._aggressive_keep_recent = aggressive_keep_recent
        self._threshold_ratio = threshold_ratio
        self._consecutive_overflow_count: int = 0

    async def on_model_exception(self, ctx: HookContext) -> None:
        exc = ctx.exception
        if exc is None or not _is_context_overflow_error(exc):
            return

        self._consecutive_overflow_count += 1
        actual_tokens, limit_tokens = _parse_token_limits(exc)
        max_attempts = self._max_recovery_attempts or _get_max_recovery_attempts()

        log.warning(
            "[ContextOverflowRecovery] Context overflow detected "
            "(attempt %d/%d) actual_tokens=%s limit_tokens=%s",
            self._consecutive_overflow_count, max_attempts,
            actual_tokens, limit_tokens,
        )

        if self._consecutive_overflow_count > max_attempts:
            await self._circuit_break(ctx)
            return

        # 计算激进压缩参数
        aggressive_keep = self._aggressive_keep_recent or _get_aggressive_keep_recent()
        ratio = self._threshold_ratio or _get_threshold_ratio()

        # 从 413 解析出 limit_tokens 时，动态算 threshold_override
        threshold_override = None
        if limit_tokens is not None:
            threshold_override = int(limit_tokens * ratio)
        else:
            config_limit = _get_config_context_limit()
            if config_limit > 0:
                threshold_override = int(config_limit * ratio)

        # 激进压缩：更小的 keep_recent_pairs + 更低的 threshold
        # 溢出恢复时必须强制压缩——已知上下文溢出，threshold 设 0 确保
        # compress_messages 不跳过。仅在解析到 limit 或 config 手动设定时
        # 才用 ratio * limit 作为目标。
        if threshold_override is None:
            threshold_override = 0
        try:
            compressed = await compress_messages(
                ctx.inputs.messages, self._llm,
                token_threshold=threshold_override,
                keep_recent_pairs=aggressive_keep,
                summary_system_prompt=_get_summary_prompt(),
            )
            ctx.inputs.messages = compressed
            log.info(
                "[ContextOverflowRecovery] Aggressive compression applied, requesting retry",
            )
        except Exception:
            log.exception("[ContextOverflowRecovery] Aggressive compression failed; retrying anyway")

        ctx.request_retry(delay=0)

    async def after_model_call(self, ctx: HookContext) -> None:
        if ctx.exception is None and self._consecutive_overflow_count > 0:
            log.info(
                "[ContextOverflowRecovery] LLM call succeeded after %d overflow recovery attempt(s)",
                self._consecutive_overflow_count,
            )
        if ctx.exception is None:
            self._consecutive_overflow_count = 0

    async def _circuit_break(self, ctx: HookContext) -> None:
        """熔断：注入消息让 LLM 产出最终回答，而非抛异常挂死。"""
        log.error(
            "[ContextOverflowRecovery] Circuit breaker triggered after %d "
            "consecutive context overflow errors",
            self._consecutive_overflow_count,
        )
        ctx.inputs.messages = list(ctx.inputs.messages) + [{
            "role": "system",
            "content": (
                "[CONTEXT_OVERFLOW] 上下文持续溢出，自动压缩恢复失败。"
                "请用当前已有信息总结回答用户，建议用户开始新会话。"
            ),
        }]
        self._consecutive_overflow_count = 0


# --- Config lazy reads ---

def _get_max_recovery_attempts() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.max_recovery_attempts


def _get_aggressive_keep_recent() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.aggressive_keep_recent


def _get_threshold_ratio() -> float:
    from twinkle.config import settings
    return settings.overflow_recovery.threshold_ratio


def _get_config_context_limit() -> int:
    from twinkle.config import settings
    return settings.overflow_recovery.context_window_limit_tokens


def _get_fallback_threshold() -> int:
    from twinkle.config import CONTEXT_TOKEN_THRESHOLD
    return CONTEXT_TOKEN_THRESHOLD


def _get_summary_prompt() -> str:
    from twinkle.config import CONTEXT_SUMMARY_PROMPT
    return CONTEXT_SUMMARY_PROMPT
