"""@hook 装饰器 — 用 before/after/exception 生命周期包裹异步方法。

适用于普通异步方法（非 async generator）。该装饰器：
1. 在实例的 HookManager 上触发 *before* 事件
2. 检查 force_finish — 若已设置则跳过方法体
3. 执行方法体
4. 触发 *after* 事件
5. 出错时：触发 *on_exception* 事件（仅观测，不重试）

工具层重试已移除（2026-08-27）：瞬时网络异常重试会重新执行有副作用的
工具方法体、无幂等保护，对写工具有重复副作用风险。模型层重试不受影响
（见 AgentLoop._inner_run_stream 的 retry 循环 + RetryHook.on_model_exception）。
on_exception 事件仍触发，供 AuditHook / RepeatToolCallDetectorHook 观测
工具异常，仅是不再重新执行方法体——异常直接 raise。

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
            不触发 exception hook。触发后异常仍向上传播（仅观测，不重试）。

    被装饰的方法必须接受 (self, ctx, ...)，其中 ctx 是一个
    HookContext。装饰器管理 ctx.event 与 before/after/exception 流程，
    以及 force_finish 信号。
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

            # 3. 执行方法体（工具层不重试 — 见模块 docstring）
            ctx.retry_attempt = 0
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
                    # 5. 触发 on_exception 事件（仅观测，不重试）
                    await hook_manager.execute(on_exception, ctx)
                raise  # 异常直接传播，不重试

        return wrapper
    return decorator
