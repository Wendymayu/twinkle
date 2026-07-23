"""Hook mechanism core types — HookEvent, AgentHook base class,
HookContext, HookInputs, and control flow signals.

Mirrors jiuwen's AgentCallbackEvent + AgentRail, adapted for Twinkle's
learning-focused reimplementation with Hook naming.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Union


class HookEvent(enum.Enum):
    """Lifecycle events in the Agent execution loop — hook trigger points.

    11 values matching jiuwen's AgentCallbackEvent one-to-one.
    8 are currently triggered; 3 are reserved for future use.
    """
    BEFORE_INVOKE = "before_invoke"
    AFTER_INVOKE = "after_invoke"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    ON_MODEL_EXCEPTION = "on_model_exception"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_TOOL_EXCEPTION = "on_tool_exception"
    # Reserved — not triggered in current AgentLoop, but kept for jiuwen mapping
    AFTER_REACT_ITERATION = "after_react_iteration"
    BEFORE_TASK_ITERATION = "before_task_iteration"
    AFTER_TASK_ITERATION = "after_task_iteration"


# Mapping from lifecycle method name → HookEvent
_EVENT_METHOD_MAP: dict[str, HookEvent] = {
    "before_invoke": HookEvent.BEFORE_INVOKE,
    "after_invoke": HookEvent.AFTER_INVOKE,
    "before_model_call": HookEvent.BEFORE_MODEL_CALL,
    "after_model_call": HookEvent.AFTER_MODEL_CALL,
    "on_model_exception": HookEvent.ON_MODEL_EXCEPTION,
    "before_tool_call": HookEvent.BEFORE_TOOL_CALL,
    "after_tool_call": HookEvent.AFTER_TOOL_CALL,
    "on_tool_exception": HookEvent.ON_TOOL_EXCEPTION,
    "after_react_iteration": HookEvent.AFTER_REACT_ITERATION,
    "before_task_iteration": HookEvent.BEFORE_TASK_ITERATION,
    "after_task_iteration": HookEvent.AFTER_TASK_ITERATION,
}


class AgentHook:
    """Base class for all agent lifecycle hooks.

    A Hook is a "capability bundle" — it groups multiple lifecycle callbacks
    into one class with a shared priority. Subclass and override only the
    methods you care about; the rest are no-ops and get_callbacks() will
    skip them automatically.

    Mirrors jiuwen's AgentRail.
    """
    priority: int = 50  # Execution order: higher number runs first

    def init(self, agent: Any) -> None:
        """Called when this hook is registered on an agent. Use for setup
        (e.g., storing agent reference, reading config)."""
        ...

    def uninit(self, agent: Any) -> None:
        """Called when this hook is unregistered. Use for teardown."""
        ...

    # 11 lifecycle callbacks — all default no-op
    async def before_invoke(self, ctx: Any) -> None: ...
    async def after_invoke(self, ctx: Any) -> None: ...
    async def before_model_call(self, ctx: Any) -> None: ...
    async def after_model_call(self, ctx: Any) -> None: ...
    async def on_model_exception(self, ctx: Any) -> None: ...
    async def before_tool_call(self, ctx: Any) -> None: ...
    async def after_tool_call(self, ctx: Any) -> None: ...
    async def on_tool_exception(self, ctx: Any) -> None: ...
    async def after_react_iteration(self, ctx: Any) -> None: ...
    async def before_task_iteration(self, ctx: Any) -> None: ...
    async def after_task_iteration(self, ctx: Any) -> None: ...

    def _is_base_method(self, method: Callable) -> bool:
        """Return True if *method* is the base-class default (not overridden).

        Compares the resolved method on this instance against the same
        method name resolved on AgentHook itself. If they're the same
        function object, the subclass didn't override it.
        """
        name = method.__func__.__name__ if hasattr(method, "__func__") else method.__name__
        base_method = getattr(AgentHook, name, None)
        if base_method is None:
            return False  # not a known lifecycle method
        actual = method.__func__ if hasattr(method, "__func__") else method
        return actual is base_method

    def get_callbacks(self) -> dict[HookEvent, Callable]:
        """Return {HookEvent: bound_method} only for lifecycle methods
        the subclass actually overrides. init/uninit are excluded.
        """
        callbacks: dict[HookEvent, Callable] = {}
        for name, event in _EVENT_METHOD_MAP.items():
            method = getattr(self, name)
            if not self._is_base_method(method):
                callbacks[event] = method
        return callbacks


# --- HookInputs (per-stage typed data) --- #


@dataclass
class InvokeInputs:
    """Inputs for BEFORE/AFTER_INVOKE events."""
    query: str
    envelope: Any  # E2AEnvelope — using Any to avoid circular import


@dataclass
class ModelCallInputs:
    """Inputs for BEFORE/AFTER/ON_MODEL_CALL events."""
    messages: list[dict]
    tools: list[dict]


@dataclass
class ToolCallInputs:
    """Inputs for BEFORE/AFTER/ON_TOOL_CALL events."""
    name: str
    args: dict
    tool_call_id: str


@dataclass
class TaskIterationInputs:
    """Inputs for BEFORE/AFTER_TASK_ITERATION events (reserved)."""
    envelope: Any


# Union type for all inputs
HookInputs = Union[InvokeInputs, ModelCallInputs, ToolCallInputs, TaskIterationInputs]


# --- Control flow signals --- #

@dataclass
class RetryRequest:
    """Signal: Hook requests retry of the current step (e.g., context
    overflow recovery compresses context then asks to re-call LLM)."""
    delay: float = 0


@dataclass
class ForceFinishRequest:
    """Signal: Hook requests skipping the current step and returning
    a result immediately (e.g., security interception)."""
    result: Any = None


class HookInterrupt(Exception):
    """Signal: Hook interrupts execution immediately (e.g., HITL approval).

    Corresponds to jiuwen's ToolInterruptException. Current roadmap
    doesn't implement permissions, but interface shape is reserved.
    """
    def __init__(self, message: str = "", data: dict | None = None):
        super().__init__(message)
        self.data = data or {}


# --- HookContext (unified data packet) --- #

@dataclass
class HookContext:
    """The context object passed to every hook callback.

    Carries: current event, stage-specific inputs, session/request IDs,
    a shared extra dict for inter-hook communication, exception info,
    and control flow signal methods.
    """
    agent: Any  # AgentLoop reference — Any to avoid circular import
    event: HookEvent
    inputs: HookInputs
    session_id: str | None
    request_id: str | None
    extra: dict = field(default_factory=dict)
    exception: Exception | None = None
    retry_attempt: int = 0

    # Internal signal fields — not part of the public API surface
    _retry_request: RetryRequest | None = field(default=None, repr=False)
    _force_finish_request: ForceFinishRequest | None = field(default=None, repr=False)

    def request_retry(self, delay: float = 0) -> None:
        """Hook requests retry of the current step after this callback finishes."""
        self._retry_request = RetryRequest(delay=delay)

    def request_force_finish(self, result: Any = None) -> None:
        """Hook requests skipping the method body and returning *result*."""
        self._force_finish_request = ForceFinishRequest(result=result)

    def consume_retry_request(self) -> RetryRequest | None:
        """Caller consumes the retry signal — returns it and clears it."""
        req = self._retry_request
        self._retry_request = None
        return req

    def consume_force_finish_request(self) -> ForceFinishRequest | None:
        """Caller consumes the force-finish signal — returns it and clears it."""
        req = self._force_finish_request
        self._force_finish_request = None
        return req
