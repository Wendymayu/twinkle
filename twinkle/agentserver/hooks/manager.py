"""HookManager — 注册、注销并执行 agent 生命周期 hook。

一个轻量分发器：按事件存放 callback，按 priority 降序排列（数值大者先跑），
顺序执行。

镜像 jiuwen 的 AgentCallbackManager + AsyncCallbackFramework，但只实现
核心：register/unregister/按 priority 排序的 execute。
不含 filter、熔断器、chain 或 transform 支持。
"""
from __future__ import annotations

import logging
from typing import Callable

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookEvent, HookInterrupt

log = logging.getLogger("twinkle.hooks.manager")


class HookManager:
    """管理单个 Agent 实例上的 AgentHook 注册与事件分发。

    register_hook() 和 unregister_hook() 是同步的，因为 AgentHook.init()
    和 uninit() 是同步的。execute() 是异步的，因为它调用异步的 hook
    callback 方法。
    """

    def __init__(self) -> None:
        # {HookEvent: [(priority, callback_method)]} — 每事件按 priority 降序排列
        self._callbacks: dict[HookEvent, list[tuple[int, Callable]]] = {}
        self._hooks: list[AgentHook] = []

    def has_callbacks_for(self, event: HookEvent) -> bool:
        """若 *event* 至少注册了一个 callback 则返回 True。"""
        return bool(self._callbacks.get(event))

    def register_hook(self, hook: AgentHook) -> None:
        """注册一个 hook：调用 init()，取出 callback，按 priority 插入排序。

        本方法是同步的，因为 AgentHook.init() 是同步的。
        """
        # HookManager 与具体 agent 无关（不持有 agent 引用）；init/uninit
        # 收到 None。需要 agent 的 hook 在 callback 中通过 ctx.agent 取得。
        hook.init(None)
        callbacks = hook.get_callbacks()
        for event, method in callbacks.items():
            entries = self._callbacks.setdefault(event, [])
            entries.append((hook.priority, method))
            # 按 priority 降序排列 — 数值大者先跑
            entries.sort(key=lambda pair: pair[0], reverse=True)
        self._hooks.append(hook)
        log.debug("registered hook %s (priority=%d, events=%s)",
                  type(hook).__name__, hook.priority,
                  [e.name for e in callbacks])

    def unregister_hook(self, hook: AgentHook) -> None:
        """注销一个 hook：调用 uninit()，移除其全部 callback。

        本方法是同步的，因为 AgentHook.uninit() 是同步的。
        """
        hook.uninit(None)  # 与具体 agent 无关 — 见 register_hook
        callbacks = hook.get_callbacks()
        for event, method in callbacks.items():
            entries = self._callbacks.get(event, [])
            # bound method 每次访问都是新对象，因此身份比较（is）无效。
            # 改为比较 __func__ 和 __self__——它们是稳定的身份标识。
            func = method.__func__
            self_obj = method.__self__
            self._callbacks[event] = [
                (priority, callback) for priority, callback in entries
                if callback.__func__ is not func or callback.__self__ is not self_obj
            ]
            # 清空空列表，使 has_callbacks_for 返回 False
            if not self._callbacks[event]:
                del self._callbacks[event]
        self._hooks = [h for h in self._hooks if h is not hook]
        log.debug("unregistered hook %s", type(hook).__name__)

    async def execute(self, event: HookEvent, ctx: HookContext) -> None:
        """按 priority 顺序（降序）执行 *event* 的全部 callback。

        在调用每个 callback 前把 ctx.event 设为 *event*。
        Fail-soft：一个 callback 失败不会阻断其他 callback — 异常被捕获并记录日志。
        控制流信号（retry/force_finish）留在 ctx 上供调用方检查 — execute() 不解释它们。
        """
        ctx.event = event
        entries = self._callbacks.get(event, [])
        for _priority, method in entries:
            try:
                await method(ctx)
            except HookInterrupt:
                raise  # HITL 控制流信号 — 必须传播给调用方
            except Exception:
                log.exception("hook callback %s failed for event %s; continuing",
                              method.__qualname__, event.name)
