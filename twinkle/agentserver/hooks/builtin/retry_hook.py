"""RetryHook — 对瞬时 model 异常重试一次。

接入既有 retry 机制（不新增循环）：在 ON_MODEL_EXCEPTION 时，若异常是瞬时的
且为第一次尝试，通过 ctx.request_retry(delay) 请求 retry。_inner_run_stream
的 model retry 循环（agent_loop.py）消费该信号并重新执行。非瞬时错误与第二次
尝试原样传播。

工具层重试已移除（2026-08-27）：瞬时网络异常重试会重新执行有副作用的方法体、
无幂等保护，对写工具有重复副作用风险。工具异常由 @hook 的 on_exception
触发观测（AuditHook / RepeatToolCallDetectorHook），不再重试、直接 raise。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import openai

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.retry")

# 值得重试的异常：瞬时的网络 / 超时 / 限流 / 服务器错误。
# 鉴权、bad-request、上下文溢出与业务错误（文件未找到、权限拒绝、
# 空命令）不在内 — 重试它们无意义或有害。
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
    asyncio.TimeoutError,
    httpx.TransportError,
)


def is_transient(exc: BaseException | None) -> bool:
    """若 *exc* 是值得重试的瞬时异常则返回 True。"""
    return isinstance(exc, TRANSIENT_EXCEPTIONS)


class RetryHook(AgentHook):
    """对瞬时 model 异常重试一次。

    priority=50 — 功能层：在安全（100）之后、观察者（0）之前。
    """

    priority = 50

    def __init__(self, max_retries: int = 1, delay: float = 1.0) -> None:
        self._max_retries = max_retries
        self._delay = delay

    async def on_model_exception(self, ctx: HookContext) -> None:
        self._maybe_request_retry(ctx)

    def _maybe_request_retry(self, ctx: HookContext) -> None:
        if ctx.retry_attempt < self._max_retries and is_transient(ctx.exception):
            log.info(
                "transient %s on attempt %d — requesting retry (delay=%.1fs)",
                type(ctx.exception).__name__, ctx.retry_attempt, self._delay,
            )
            ctx.request_retry(delay=self._delay)
