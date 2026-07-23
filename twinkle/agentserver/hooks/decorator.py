"""@hook decorator — wraps async methods with before/after/exception lifecycle.

For regular async methods (not async generators). The decorator:
1. Triggers the *before* event on the instance's HookManager
2. Checks force_finish — skips method body if set
3. Executes the method body
4. Triggers the *after* event
5. On exception: triggers *on_exception* event, checks retry request,
   re-executes if retry requested (max 3 attempts)

For async generators (like AgentLoop.run_stream), use manual
self._hooks.execute() calls instead — @hook cannot wrap generators.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable

from twinkle.agentserver.hooks.base import HookEvent, HookContext, HookInterrupt

log = logging.getLogger("twinkle.hooks.decorator")

_MAX_RETRY_ATTEMPTS = 3


def hook(
    before: HookEvent,
    after: HookEvent,
    on_exception: HookEvent | None = None,
) -> Callable:
    """Decorator that wraps an async method with hook lifecycle.

    Args:
        before: Event triggered before the method body executes.
        after: Event triggered after the method body completes (on success).
        on_exception: Event triggered if the method raises. None means
            exceptions propagate without triggering an exception hook.

    The decorated method must accept (self, ctx, ...) where ctx is a
    HookContext. The decorator manages ctx.event and the before/after/
    exception flow, plus force_finish and retry signals.
    """
    def decorator(method: Callable) -> Callable:
        @functools.wraps(method)
        async def wrapper(self: Any, ctx: HookContext, *args: Any, **kwargs: Any) -> Any:
            hooks = self._hooks  # HookManager on the instance

            # 1. Trigger before event
            await hooks.execute(before, ctx)

            # 2. Check force_finish — skip method body if set
            ff = ctx.consume_force_finish_request()
            if ff is not None:
                return ff.result

            # 3. Execute method body (with retry support)
            for attempt in range(_MAX_RETRY_ATTEMPTS + 1):
                ctx.retry_attempt = attempt
                ctx.exception = None
                try:
                    result = await method(self, ctx, *args, **kwargs)
                    # 4. Trigger after event on success
                    await hooks.execute(after, ctx)
                    return result
                except asyncio.CancelledError:
                    raise  # never interfere with cancellation
                except HookInterrupt:
                    raise  # interrupt propagates immediately
                except Exception as exc:
                    ctx.exception = exc
                    if on_exception is not None:
                        # 5. Trigger on_exception event
                        await hooks.execute(on_exception, ctx)
                        # Check retry request
                        retry = ctx.consume_retry_request()
                        if retry is not None and attempt < _MAX_RETRY_ATTEMPTS:
                            if retry.delay > 0:
                                await asyncio.sleep(retry.delay)
                            continue  # retry the method body
                    raise  # no retry or max attempts exceeded

        return wrapper
    return decorator
