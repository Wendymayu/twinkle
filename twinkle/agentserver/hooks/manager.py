"""HookManager — register, unregister, and execute agent lifecycle hooks.

A lightweight dispatcher that stores callbacks per event, sorted by
priority (descending — higher runs first), and executes them sequentially.

Mirrors jiuwen's AgentCallbackManager + AsyncCallbackFramework, but only
implements the core: register/unregister/priority-sorted execute.
No filter, circuit breaker, chain, or transform support.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookEvent

log = logging.getLogger("twinkle.hooks.manager")


class HookManager:
    """Manages AgentHook registration and event dispatch for one Agent instance.

    register_hook() and unregister_hook() are sync because AgentHook.init()
    and uninit() are sync. execute() is async because it calls async hook
    callback methods.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        # {HookEvent: [(priority, callback_method)]} — sorted descending per event
        self._callbacks: dict[HookEvent, list[tuple[int, Callable]]] = {}
        self._hooks: list[AgentHook] = []

    def has_callbacks_for(self, event: HookEvent) -> bool:
        """Return True if at least one callback is registered for *event*."""
        return bool(self._callbacks.get(event))

    def register_hook(self, hook: AgentHook) -> None:
        """Register a hook: call init(), get callbacks, insert sorted.

        This is a sync method because AgentHook.init() is sync.
        """
        hook.init(self._agent)
        callbacks = hook.get_callbacks()
        for event, method in callbacks.items():
            entries = self._callbacks.setdefault(event, [])
            entries.append((hook.priority, method))
            # Sort descending by priority — higher runs first
            entries.sort(key=lambda pair: pair[0], reverse=True)
        self._hooks.append(hook)
        log.debug("registered hook %s (priority=%d, events=%s)",
                  type(hook).__name__, hook.priority,
                  [e.name for e in callbacks])

    def unregister_hook(self, hook: AgentHook) -> None:
        """Unregister a hook: call uninit(), remove all its callbacks.

        This is a sync method because AgentHook.uninit() is sync.
        """
        hook.uninit(self._agent)
        callbacks = hook.get_callbacks()
        for event, method in callbacks.items():
            entries = self._callbacks.get(event, [])
            # Bound methods are fresh objects on each access, so identity
            # comparison (is) doesn't work. Compare __func__ and __self__
            # instead, which are stable identity markers.
            func = method.__func__
            self_obj = method.__self__
            self._callbacks[event] = [
                (pri, cb) for pri, cb in entries
                if cb.__func__ is not func or cb.__self__ is not self_obj
            ]
            # Clean up empty lists so has_callbacks_for returns False
            if not self._callbacks[event]:
                del self._callbacks[event]
        self._hooks = [h for h in self._hooks if h is not hook]
        log.debug("unregistered hook %s", type(hook).__name__)

    async def execute(self, event: HookEvent, ctx: HookContext) -> None:
        """Execute all callbacks for *event*, in priority order (descending).

        Sets ctx.event to *event* before calling each callback.
        Fail-soft: one failing callback doesn't stop others — exceptions
        are caught and logged.
        Control flow signals (retry/force_finish) are left on ctx for
        the caller to check — execute() does not interpret them.
        """
        ctx.event = event
        entries = self._callbacks.get(event, [])
        for _pri, method in entries:
            try:
                await method(ctx)
            except Exception:
                log.exception("hook callback %s failed for event %s; continuing",
                              method.__qualname__, event.name)
