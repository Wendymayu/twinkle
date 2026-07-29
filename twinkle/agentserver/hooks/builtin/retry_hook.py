"""RetryHook — retry transient model/tool exceptions once.

Plugs into the existing retry machinery (no new loop): on ON_MODEL_EXCEPTION /
ON_TOOL_EXCEPTION, if the exception is transient and this is the first attempt,
request a retry via ctx.request_retry(delay). The @hook decorator (tool path)
and _inner_run_stream's model retry loop (agent_loop.py) consume the signal and
re-execute. Non-transient errors and second attempts propagate untouched.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import openai

from twinkle.agentserver.hooks.base import AgentHook, HookContext

log = logging.getLogger("twinkle.hooks.retry")

# Exceptions worth retrying: transient network / timeout / rate-limit / server
# errors. Auth, bad-request, context-overflow and business errors (file not
# found, permission denied, empty command) are NOT here — retrying them is
# pointless or harmful.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
    asyncio.TimeoutError,
    httpx.TransportError,
)


def is_transient(exc: BaseException | None) -> bool:
    """Return True if *exc* is a transient exception worth retrying."""
    return isinstance(exc, TRANSIENT_EXCEPTIONS)


class RetryHook(AgentHook):
    """Retry transient model + tool exceptions once.

    priority=50 — functional layer: after security (100), before observers (0).
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
