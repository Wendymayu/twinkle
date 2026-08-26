"""@hook 装饰器 — 用 before/after/exception 生命周期包裹异步方法。

适用于普通异步方法（非 async generator）。该装饰器：
1. 在实例的 HookManager 上触发 *before* 事件
2. 检查 force_finish — 若已设置则跳过方法体
3. 执行方法体
4. 触发 *after* 事件
5. 出错时：触发 *on_exception* 事件，检查 retry 请求，
   若请求了 retry 则重新执行（最多 3 次）

对于 async generator（如 AgentLoop.run_stream），改用手动
self._hook_manager.execute() 调用 — @hook 无法包裹 generator。
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
    """用 hook 生命周期包裹异步方法的装饰器。

    Args:
        before: 方法体执行前触发的事件。
        after: 方法体成功完成后触发的事件。
        on_exception: 方法抛出异常时触发的事件。None 表示异常直接传播，
            不触发 exception hook。

    被装饰的方法必须接受 (self, ctx, ...)，其中 ctx 是一个
    HookContext。装饰器管理 ctx.event 与 before/after/exception 流程，
    以及 force_finish 和 retry 信号。
    """
    def decorator(method: Callable) -> Callable:
        @functools.wraps(method)
        async def wrapper(self: Any, ctx: HookContext, *args: Any, **kwargs: Any) -> Any:
            hook_manager = self._hook_manager  # 实例上的 HookManager

            # 1. 触发 before 事件
            await hook_manager.execute(before, ctx)

            # 2. 检查 force_finish — 若已设置则跳过方法体
            force_finish = ctx.consume_force_finish_request()
            if force_finish is not None:
                return force_finish.result

            # 3. 执行方法体（带 retry 支持）
            for attempt in range(_MAX_RETRY_ATTEMPTS + 1):
                ctx.retry_attempt = attempt
                ctx.exception = None
                try:
                    result = await method(self, ctx, *args, **kwargs)
                    # 为 after 事件 hook（如 RepeatToolCallDetectorHook）存结果
                    ctx.extra["_tool_result"] = result
                    # 4. 成功时触发 after 事件
                    await hook_manager.execute(after, ctx)
                    return result
                except asyncio.CancelledError:
                    raise  # 绝不干扰取消
                except HookInterrupt:
                    raise  # interrupt 立即传播
                except Exception as exc:
                    ctx.exception = exc
                    if on_exception is not None:
                        # 5. 触发 on_exception 事件
                        await hook_manager.execute(on_exception, ctx)
                        # 检查 retry 请求
                        retry_request = ctx.consume_retry_request()
                        if retry_request is not None and attempt < _MAX_RETRY_ATTEMPTS:
                            if retry_request.delay > 0:
                                await asyncio.sleep(retry_request.delay)
                            continue  # 重试方法体
                    raise  # 无 retry 或已达最大次数

        return wrapper
    return decorator
