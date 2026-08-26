"""RetryHook — 对瞬时 model/tool 异常重试一次。

接入既有 retry 机制（不新增循环）：在 ON_MODEL_EXCEPTION /
ON_TOOL_EXCEPTION 时，若异常是瞬时的且为第一次尝试，
通过 ctx.request_retry(delay) 请求 retry。@hook 装饰器（tool 路径）
与 _inner_run_stream 的 model retry 循环（agent_loop.py）消费该信号并
重新执行。非瞬时错误与第二次尝试原样传播。
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
    """对瞬时 model + tool 异常重试一次。

    priority=50 — 功能层：在安全（100）之后、观察者（0）之前。
    """

    priority = 50

    def __init__(self, max_retries: int = 1, delay: float = 1.0) -> None:
        self._max_retries = max_retries
        self._delay = delay

    async def on_model_exception(self, ctx: HookContext) -> None:
        self._maybe_request_retry(ctx)

    async def on_tool_exception(self, ctx: HookContext) -> None:
        self._maybe_request_retry(ctx)

    def _maybe_request_retry(self, ctx: HookContext) -> None:
        if ctx.retry_attempt < self._max_retries and is_transient(ctx.exception):
            log.info(
                "transient %s on attempt %d — requesting retry (delay=%.1fs)",
                type(ctx.exception).__name__, ctx.retry_attempt, self._delay,
            )
            ctx.request_retry(delay=self._delay)
