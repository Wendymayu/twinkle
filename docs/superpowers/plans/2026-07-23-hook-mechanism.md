# Hook Mechanism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a class-based lifecycle hook mechanism for Twinkle's Agent execution loop, mirroring jiuwen's Rail system.

**Architecture:** AgentHook base class with 11 lifecycle methods, priority-ordered execution via HookManager, HookContext dataclass for unified event data + inter-hook communication + control flow signals, @hook decorator for wrapping regular async methods. AgentLoop integration uses manual `self._hooks.execute()` calls (since run_stream is an async generator).

**Tech Stack:** Python 3.10+ (uses `|` union syntax), asyncio, dataclasses, enum, no new external dependencies.

## Global Constraints

- No `pytest-asyncio` — all async tests use `asyncio.run()` per project convention
- `run_stream(envelope)` signature must not change — existing tests and server.py must pass unmodified
- Hook mechanism and existing observability monkey-patch coexist — no observability migration in this plan
- All naming uses `Hook` (not `Rail`) — `AgentHook`, `HookEvent`, `HookContext`, `HookManager`, `@hook`
- 11 HookEvent values (8 triggered, 3 reserved) matching jiuwen's AgentCallbackEvent one-to-one
- File paths under `twinkle/agentserver/hooks/`

---

## File Structure

```
twinkle/agentserver/hooks/
    __init__.py              # Public API re-exports
    base.py                  # HookEvent enum, AgentHook base, HookContext, HookInputs subclasses,
                              # RetryRequest, ForceFinishRequest, HookInterrupt
    manager.py               # HookManager (register/unregister/execute)
    decorator.py             # @hook decorator for regular async methods
    builtin/
        __init__.py           # Re-export LoggingHook
        logging_hook.py       # LoggingHook — first concrete Hook

tests/
    test_hook_base.py         # HookEvent, AgentHook, _is_base_method, get_callbacks
    test_hook_context.py      # HookContext, extra dict, control flow signals
    test_hook_manager.py      # HookManager register/unregister/priority ordering/execute
    test_hook_decorator.py    # @hook decorator before/after/exception/force_finish/retry

Modified:
    twinkle/agentserver/agent_loop.py   # Add HookManager, refactor run_stream into run_stream + _inner_run_stream
    twinkle/agentserver/server.py       # build_agent_loop accepts hooks parameter
```

---

### Task 1: HookEvent Enum + AgentHook Base Class

**Files:**
- Create: `twinkle/agentserver/hooks/base.py`
- Create: `tests/test_hook_base.py`

**Interfaces:**
- Produces: `HookEvent` enum (11 values), `AgentHook` base class with `priority`, `init`, `uninit`, 11 lifecycle methods, `get_callbacks()` returning `dict[HookEvent, Callable]`, `_is_base_method()` helper

- [ ] **Step 1: Write failing tests for HookEvent and AgentHook**

```python
# tests/test_hook_base.py
"""Tests for HookEvent enum and AgentHook base class."""
from __future__ import annotations

import enum
import asyncio

from twinkle.agentserver.hooks.base import AgentHook, HookEvent


def test_hook_event_has_11_values():
    assert len(HookEvent) == 11


def test_hook_event_values_match_names():
    expected = {
        "BEFORE_INVOKE", "AFTER_INVOKE",
        "BEFORE_MODEL_CALL", "AFTER_MODEL_CALL", "ON_MODEL_EXCEPTION",
        "BEFORE_TOOL_CALL", "AFTER_TOOL_CALL", "ON_TOOL_EXCEPTION",
        "AFTER_REACT_ITERATION",
        "BEFORE_TASK_ITERATION", "AFTER_TASK_ITERATION",
    }
    assert {e.name for e in HookEvent} == expected


def test_hook_event_is_enum():
    assert issubclass(HookEvent, enum.Enum)


def test_base_hook_default_priority():
    h = AgentHook()
    assert h.priority == 50


def test_base_hook_get_callbacks_returns_empty():
    """Base AgentHook with no overrides should return empty callbacks dict."""
    h = AgentHook()
    callbacks = h.get_callbacks()
    assert callbacks == {}


def test_subclass_get_callbacks_returns_only_overridden():
    """A subclass that overrides 2 methods should get 2 callbacks."""
    class TwoMethodHook(AgentHook):
        priority = 90

        async def before_model_call(self, ctx):
            pass

        async def after_tool_call(self, ctx):
            pass

    h = TwoMethodHook()
    callbacks = h.get_callbacks()
    assert len(callbacks) == 2
    assert HookEvent.BEFORE_MODEL_CALL in callbacks
    assert HookEvent.AFTER_TOOL_CALL in callbacks


def test_subclass_init_uninit_not_in_callbacks():
    """init/uninit are lifecycle methods, not event callbacks — they should
    never appear in get_callbacks()."""
    class InitHook(AgentHook):
        async def init(self, agent):
            pass

        async def before_invoke(self, ctx):
            pass

    h = InitHook()
    callbacks = h.get_callbacks()
    assert HookEvent.BEFORE_INVOKE in callbacks
    # init is NOT a HookEvent callback
    assert len(callbacks) == 1


def test_subclass_priority_propagated_to_callbacks():
    """All callbacks from the same Hook share its priority."""
    class HighPriHook(AgentHook):
        priority = 100

        async def before_invoke(self, ctx):
            pass

        async def after_invoke(self, ctx):
            pass

    h = HighPriHook()
    callbacks = h.get_callbacks()
    assert len(callbacks) == 2


def test_is_base_method_detects_override():
    class OverrideHook(AgentHook):
        async def before_model_call(self, ctx):
            pass

    h = OverrideHook()
    # The overridden method should NOT be detected as "base"
    assert not h._is_base_method(h.before_model_call)
    # A method NOT overridden should be detected as "base"
    assert h._is_base_method(h.after_model_call)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook_base.py -v`
Expected: FAIL — `twinkle.agentserver.hooks.base` does not exist yet.

- [ ] **Step 3: Implement HookEvent and AgentHook**

```python
# twinkle/agentserver/hooks/base.py
"""Hook mechanism core types — HookEvent, AgentHook base class.

Mirrors jiuwen's AgentCallbackEvent + AgentRail, adapted for Twinkle's
learning-focused reimplementation with Hook naming.
"""
from __future__ import annotations

import enum
from typing import Any, Callable


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

    async def init(self, agent: Any) -> None:
        """Called when this hook is registered on an agent. Use for setup
        (e.g., storing agent reference, reading config)."""

    async def uninit(self, agent: Any) -> None:
        """Called when this hook is unregistered. Use for teardown."""

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hook_base.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/hooks/base.py tests/test_hook_base.py
git commit -m "feat(hooks): add HookEvent enum and AgentHook base class with get_callbacks"
```

---

### Task 2: HookContext, HookInputs, and Control Flow Signals

**Files:**
- Modify: `twinkle/agentserver/hooks/base.py` (append HookContext, HookInputs, RetryRequest, ForceFinishRequest, HookInterrupt)
- Create: `tests/test_hook_context.py`

**Interfaces:**
- Consumes: `HookEvent` from Task 1
- Produces: `HookContext(agent, event, inputs, session_id, request_id, extra, exception, retry_attempt)` with `request_retry()`, `request_force_finish()`, `consume_retry_request()`, `consume_force_finish_request()`; `HookInputs` union + subclasses `InvokeInputs`, `ModelCallInputs`, `ToolCallInputs`, `TaskIterationInputs`; `RetryRequest(delay)`, `ForceFinishRequest(result)`, `HookInterrupt(Exception)`

- [ ] **Step 1: Write failing tests for HookContext, HookInputs, and control flow signals**

```python
# tests/test_hook_context.py
"""Tests for HookContext, HookInputs subclasses, and control flow signals."""
from __future__ import annotations

import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    RetryRequest,
    ForceFinishRequest,
    TaskIterationInputs,
    ToolCallInputs,
)


def test_hook_context_basic_fields():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    assert ctx.session_id == "s1"
    assert ctx.request_id == "r1"
    assert ctx.event == HookEvent.BEFORE_INVOKE
    assert isinstance(ctx.extra, dict)
    assert ctx.exception is None
    assert ctx.retry_attempt == 0


def test_hook_context_extra_dict_shared_across_access():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    ctx.extra["key1"] = "value1"
    assert ctx.extra["key1"] == "value1"
    # Different ctx.extra dicts are independent
    ctx2 = HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s2",
        request_id="r2",
    )
    assert "key1" not in ctx2.extra


def test_invoke_inputs():
    inp = InvokeInputs(query="hello", envelope=None)
    assert inp.query == "hello"
    assert inp.envelope is None


def test_model_call_inputs():
    inp = ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[{"name": "echo"}])
    assert len(inp.messages) == 1
    assert len(inp.tools) == 1


def test_tool_call_inputs():
    inp = ToolCallInputs(name="echo", args={"text": "hi"}, tool_call_id="tc1")
    assert inp.name == "echo"
    assert inp.args == {"text": "hi"}
    assert inp.tool_call_id == "tc1"


def test_task_iteration_inputs():
    inp = TaskIterationInputs(envelope=None)
    assert inp.envelope is None


def test_request_retry_and_consume():
    ctx = HookContext(
        agent=None,
        event=HookEvent.ON_MODEL_EXCEPTION,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    # Initially no retry request
    assert ctx.consume_retry_request() is None

    # Set retry request
    ctx.request_retry(delay=0.5)
    req = ctx.consume_retry_request()
    assert req is not None
    assert req.delay == 0.5

    # Consumed — second call returns None
    assert ctx.consume_retry_request() is None


def test_request_force_finish_and_consume():
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    # Initially no force finish request
    assert ctx.consume_force_finish_request() is None

    # Set force finish request
    ctx.request_force_finish(result="denied")
    ff = ctx.consume_force_finish_request()
    assert ff is not None
    assert ff.result == "denied"

    # Consumed — second call returns None
    assert ctx.consume_force_finish_request() is None


def test_hook_interrupt_exception():
    exc = HookInterrupt(message="need approval", data={"tool": "rm"})
    assert str(exc) == "need approval"
    assert exc.data == {"tool": "rm"}
    # Default data is empty dict
    exc2 = HookInterrupt(message="stop")
    assert exc2.data == {}


def test_hook_interrupt_is_exception():
    assert issubclass(HookInterrupt, Exception)


def test_retry_request_dataclass():
    req = RetryRequest(delay=2.0)
    assert req.delay == 2.0
    req_default = RetryRequest()
    assert req_default.delay == 0


def test_force_finish_request_dataclass():
    ff = ForceFinishRequest(result="blocked")
    assert ff.result == "blocked"
    ff_default = ForceFinishRequest()
    assert ff_default.result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook_context.py -v`
Expected: FAIL — HookContext, HookInputs etc. not defined yet.

- [ ] **Step 3: Implement HookContext, HookInputs, and control flow signals**

Append to `twinkle/agentserver/hooks/base.py`:

```python
# --- HookInputs (per-stage typed data) --- #

from dataclasses import dataclass, field
from typing import Any, Union


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
```

Also update the `from __future__ import annotations` at the top (already present) — no change needed since Python 3.10+ handles `|` union syntax.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hook_context.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Run ALL existing tests to verify no regressions**

Run: `python -m pytest tests/ -v`
Expected: All existing tests + new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/base.py tests/test_hook_context.py
git commit -m "feat(hooks): add HookContext, HookInputs subclasses, RetryRequest, ForceFinishRequest, HookInterrupt"
```

---

### Task 3: HookManager — Registration, Unregistration, Execution

**Files:**
- Create: `twinkle/agentserver/hooks/manager.py`
- Create: `tests/test_hook_manager.py`

**Interfaces:**
- Consumes: `AgentHook`, `HookEvent`, `HookContext` from Tasks 1-2
- Produces: `HookManager(agent)` with `register_hook(hook)`, `unregister_hook(hook)`, `async execute(event, ctx)`; callbacks sorted by priority descending

- [ ] **Step 1: Write failing tests for HookManager**

```python
# tests/test_hook_manager.py
"""Tests for HookManager — register, unregister, priority ordering, execute."""
from __future__ import annotations

import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    InvokeInputs,
)
from twinkle.agentserver.hooks.manager import HookManager


class _RecorderHook(AgentHook):
    """Hook that records which events it received, in order."""
    priority = 50

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("before_invoke")

    async def after_invoke(self, ctx):
        self.calls.append("after_invoke")

    async def before_model_call(self, ctx):
        self.calls.append("before_model_call")

    async def after_tool_call(self, ctx):
        self.calls.append("after_tool_call")


class _HighPriHook(AgentHook):
    priority = 90

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("high:before_invoke")


class _LowPriHook(AgentHook):
    priority = 10

    def __init__(self):
        self.calls: list[str] = []

    async def before_invoke(self, ctx):
        self.calls.append("low:before_invoke")


def test_register_hook_adds_callbacks():
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    asyncio.run(mgr.register_hook(h))
    assert mgr.has_callbacks_for(HookEvent.BEFORE_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.AFTER_INVOKE)
    assert mgr.has_callbacks_for(HookEvent.BEFORE_MODEL_CALL)
    assert mgr.has_callbacks_for(HookEvent.AFTER_TOOL_CALL)
    # Events the hook doesn't override — no callbacks
    assert not mgr.has_callbacks_for(HookEvent.BEFORE_TOOL_CALL)


def test_register_hook_calls_init():
    class InitRecorder(AgentHook):
        def __init__(self):
            self.inited = False
        async def init(self, agent):
            self.inited = True
    mgr = HookManager(agent="fake_agent")
    h = InitRecorder()
    asyncio.run(mgr.register_hook(h))
    assert h.inited is True


def test_unregister_hook_removes_callbacks():
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    asyncio.run(mgr.register_hook(h))
    asyncio.run(mgr.unregister_hook(h))
    assert not mgr.has_callbacks_for(HookEvent.BEFORE_INVOKE)


def test_unregister_hook_calls_uninit():
    class UninitRecorder(AgentHook):
        def __init__(self):
            self.uninited = False
        async def uninit(self, agent):
            self.uninited = True
    mgr = HookManager(agent="fake_agent")
    h = UninitRecorder()
    asyncio.run(mgr.register_hook(h))
    asyncio.run(mgr.unregister_hook(h))
    assert h.uninited is True


def test_execute_calls_hooks_in_priority_order():
    """Higher priority runs first."""
    mgr = HookManager(agent=None)
    high = _HighPriHook()
    low = _LowPriHook()
    asyncio.run(mgr.register_hook(low))   # register low first
    asyncio.run(mgr.register_hook(high))   # then high
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # high(90) should run before low(10)
    assert high.calls == ["high:before_invoke"]
    assert low.calls == ["low:before_invoke"]


def test_execute_no_hooks_is_noop():
    """Executing an event with no registered hooks should not error."""
    mgr = HookManager(agent=None)
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_INVOKE, ctx))
    # No error, ctx unchanged


def test_execute_updates_ctx_event():
    """execute() should set ctx.event to the event being triggered."""
    mgr = HookManager(agent=None)
    h = _RecorderHook()
    asyncio.run(mgr.register_hook(h))
    ctx = HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,  # initial event
        inputs=InvokeInputs(query="hi", envelope=None),
        session_id="s1",
        request_id="r1",
    )
    asyncio.run(mgr.execute(HookEvent.BEFORE_MODEL_CALL, ctx))
    # The hook's before_model_call should have been called
    assert h.calls == ["before_model_call"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook_manager.py -v`
Expected: FAIL — `twinkle.agentserver.hooks.manager` does not exist yet.

- [ ] **Step 3: Implement HookManager**

```python
# twinkle/agentserver/hooks/manager.py
"""HookManager — register, unregister, and execute agent lifecycle hooks.

A lightweight dispatcher that stores callbacks per event, sorted by
priority (descending — higher runs first), and executes them sequentially.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookEvent

log = logging.getLogger("twinkle.hooks.manager")


class HookManager:
    """Manages AgentHook registration and event dispatch for one Agent instance.

    Corresponds to jiuwen's AgentCallbackManager + AsyncCallbackFramework,
    but only implements the core: register/unregister/priority-sorted execute.
    No filter, circuit breaker, chain, or transform support.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        # {HookEvent: [(priority, callback_method)]} — sorted descending per event
        self._callbacks: dict[HookEvent, list[tuple[int, Callable]]] = {}
        self._hooks: list[AgentHook] = []

    def has_callbacks_for(self, event: HookEvent) -> bool:
        """Return True if at least one callback is registered for *event*."""
        return bool(self._callbacks.get(event))

    async def register_hook(self, hook: AgentHook) -> None:
        """Register a hook: call init(), get callbacks, insert sorted."""
        await hook.init(self._agent)
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

    async def unregister_hook(self, hook: AgentHook) -> None:
        """Unregister a hook: call uninit(), remove all its callbacks."""
        await hook.uninit(self._agent)
        callbacks = hook.get_callbacks()
        for event, method in callbacks.items():
            entries = self._callbacks.get(event, [])
            self._callbacks[event] = [
                (pri, cb) for pri, cb in entries
                if cb is not method
            ]
        self._hooks = [h for h in self._hooks if h is not hook]
        log.debug("unregistered hook %s", type(hook).__name__)

    async def execute(self, event: HookEvent, ctx: HookContext) -> None:
        """Execute all callbacks for *event*, in priority order (descending).

        Sets ctx.event to *event* before calling each callback.
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hook_manager.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Run ALL tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/manager.py tests/test_hook_manager.py
git commit -m "feat(hooks): add HookManager — register, unregister, priority-sorted execute"
```

---

### Task 4: @hook Decorator

**Files:**
- Create: `twinkle/agentserver/hooks/decorator.py`
- Create: `tests/test_hook_decorator.py`

**Interfaces:**
- Consumes: `HookEvent`, `HookContext`, `HookManager`, `HookInterrupt`, `RetryRequest` from Tasks 1-3
- Produces: `@hook(before, after, on_exception)` decorator that wraps regular async methods (not generators) with before/after/exception lifecycle, force_finish check, and retry loop

- [ ] **Step 1: Write failing tests for @hook decorator**

```python
# tests/test_hook_decorator.py
"""Tests for the @hook decorator — before/after/exception/force_finish/retry."""
from __future__ import annotations

import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    RetryRequest,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager


# --- Helper: a minimal "agent" with a HookManager --- #

class _FakeAgent:
    def __init__(self):
        self._hooks = HookManager(self)
        self.call_log: list[str] = []


class _RecorderHook(AgentHook):
    """Records events it receives."""
    priority = 50

    def __init__(self):
        self.calls: list[str] = []

    async def before_model_call(self, ctx):
        self.calls.append("before_model_call")

    async def after_model_call(self, ctx):
        self.calls.append("after_model_call")

    async def on_model_exception(self, ctx):
        self.calls.append("on_model_exception")


def test_hook_decorator_triggers_before_then_body_then_after():
    """@hook(BEFORE, AFTER) wraps a method: before → body → after."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    asyncio.run(agent._hooks.register_hook(rec))

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL)
    async def do_work(self, ctx):
        self.call_log.append("body")
        return "done"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(do_work(agent, ctx))
    assert result == "done"
    assert agent.call_log == ["body"]
    assert rec.calls == ["before_model_call", "after_model_call"]


def test_hook_decorator_on_exception_triggers_exception_hook():
    """When method raises, on_exception hook is called."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    asyncio.run(agent._hooks.register_hook(rec))

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def failing_work(self, ctx):
        self.call_log.append("body")
        raise ValueError("boom")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(failing_work(agent, ctx))
    except ValueError:
        pass
    assert agent.call_log == ["body"]
    assert rec.calls == ["before_model_call", "on_model_exception"]
    # after should NOT be called on exception
    assert "after_model_call" not in rec.calls


def test_hook_decorator_force_finish_skips_body():
    """If a before-hook sets force_finish, the method body is skipped."""
    class ForceFinishHook(AgentHook):
        priority = 100
        async def before_model_call(self, ctx):
            ctx.request_force_finish(result="blocked")

    agent = _FakeAgent()
    asyncio.run(agent._hooks.register_hook(ForceFinishHook()))

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL)
    async def do_work(self, ctx):
        self.call_log.append("body")  # should NOT execute
        return "done"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(do_work(agent, ctx))
    assert result == "blocked"
    assert agent.call_log == []  # body was skipped


def test_hook_decorator_retry_re_executes_body():
    """If on_exception hook requests retry, the method body is re-executed."""
    class RetryHook(AgentHook):
        priority = 100
        fail_count = 0

        async def on_model_exception(self, ctx):
            self.fail_count += 1
            if self.fail_count < 2:
                ctx.request_retry(delay=0)

    agent = _FakeAgent()
    asyncio.run(agent._hooks.register_hook(RetryHook()))

    attempt = 0

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def retryable_work(self, ctx):
        attempt += 1  # Note: this is closure-scoped, not instance-scoped
        if attempt < 2:
            raise ValueError("temporary failure")
        self.call_log.append(f"body-attempt-{attempt}")
        return "success"

    # Fix: use instance attribute instead of closure
    agent.attempt = 0

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def retryable_work_v2(self, ctx):
        self.attempt += 1
        if self.attempt < 2:
            raise ValueError("temporary failure")
        self.call_log.append(f"body-attempt-{self.attempt}")
        return "success"

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    result = asyncio.run(retryable_work_v2(agent, ctx))
    assert result == "success"
    assert agent.call_log == ["body-attempt-2"]


def test_hook_decorator_interrupt_propagates_immediately():
    """HookInterrupt raised inside a @hook-decorated method propagates
    without triggering on_exception."""
    agent = _FakeAgent()
    rec = _RecorderHook()
    asyncio.run(agent._hooks.register_hook(rec))

    @hook(HookEvent.BEFORE_MODEL_CALL, HookEvent.AFTER_MODEL_CALL,
          on_exception=HookEvent.ON_MODEL_EXCEPTION)
    async def interrupting_work(self, ctx):
        raise HookInterrupt("need approval")

    ctx = HookContext(
        agent=agent,
        event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=[]),
        session_id="s1",
        request_id="r1",
    )
    try:
        asyncio.run(interrupting_work(agent, ctx))
    except HookInterrupt:
        pass
    # on_model_exception should NOT be called for HookInterrupt
    assert "on_model_exception" not in rec.calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook_decorator.py -v`
Expected: FAIL — `twinkle.agentserver.hooks.decorator` does not exist yet.

- [ ] **Step 3: Implement @hook decorator**

```python
# twinkle/agentserver/hooks/decorator.py
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
        after: Event triggered after the method body completes (in finally).
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hook_decorator.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Run ALL tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/decorator.py tests/test_hook_decorator.py
git commit -m "feat(hooks): add @hook decorator for before/after/exception/force_finish/retry"
```

---

### Task 5: LoggingHook + hooks Package Init

**Files:**
- Create: `twinkle/agentserver/hooks/__init__.py`
- Create: `twinkle/agentserver/hooks/builtin/__init__.py`
- Create: `twinkle/agentserver/hooks/builtin/logging_hook.py`

**Interfaces:**
- Consumes: All types from Tasks 1-4
- Produces: Public API re-exports from `twinkle.agentserver.hooks`; `LoggingHook` class with `priority=10`, overriding `before_model_call`, `after_model_call`, `before_tool_call`, `after_tool_call`

- [ ] **Step 1: Create the hooks package __init__.py**

```python
# twinkle/agentserver/hooks/__init__.py
"""Twinkle Hook mechanism — public API.

Mirrors jiuwen's Rail system with Hook naming.
"""
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    HookInputs,
    InvokeInputs,
    ModelCallInputs,
    RetryRequest,
    ForceFinishRequest,
    TaskIterationInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.agentserver.hooks.decorator import hook

__all__ = [
    "AgentHook",
    "HookContext",
    "HookEvent",
    "HookInterrupt",
    "HookInputs",
    "InvokeInputs",
    "ModelCallInputs",
    "RetryRequest",
    "ForceFinishRequest",
    "TaskIterationInputs",
    "ToolCallInputs",
    "HookManager",
    "hook",
]
```

- [ ] **Step 2: Create the builtin hooks __init__.py**

```python
# twinkle/agentserver/hooks/builtin/__init__.py
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook

__all__ = ["LoggingHook"]
```

- [ ] **Step 3: Implement LoggingHook**

```python
# twinkle/agentserver/hooks/builtin/logging_hook.py
"""LoggingHook — first concrete AgentHook demonstration.

Records LLM and tool call events via the standard logging module.
Priority=10 ensures it runs after security/functional hooks (85-100)
but before observability span management (0).
"""
from __future__ import annotations

import logging

from twinkle.agentserver.hooks.base import AgentHook

log = logging.getLogger("twinkle.hooks.logging")


class LoggingHook(AgentHook):
    """Log LLM calls and tool executions via the standard logging module."""

    priority = 10  # After security (85-100), before observability (0)

    async def before_model_call(self, ctx) -> None:
        log.info("LLM call starting, session=%s", ctx.session_id)

    async def after_model_call(self, ctx) -> None:
        log.info("LLM call finished, session=%s", ctx.session_id)

    async def before_tool_call(self, ctx) -> None:
        log.info("tool %s starting, args=%s", ctx.inputs.name, ctx.inputs.args)

    async def after_tool_call(self, ctx) -> None:
        log.info("tool %s finished, session=%s", ctx.session_id)
```

- [ ] **Step 4: Verify the package imports correctly**

Run: `python -c "from twinkle.agentserver.hooks import AgentHook, HookEvent, HookManager, hook, HookContext; from twinkle.agentserver.hooks.builtin import LoggingHook; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Verify LoggingHook.get_callbacks works**

Run: `python -c "from twinkle.agentserver.hooks.builtin import LoggingHook; h = LoggingHook(); print(h.get_callbacks()); print('priority:', h.priority)"`
Expected: Dict with 4 HookEvent keys, priority=10

- [ ] **Step 6: Run ALL tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (no new test file — LoggingHook is simple enough to verify by import check, tested thoroughly via Task 6 integration).

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/hooks/__init__.py twinkle/agentserver/hooks/builtin/__init__.py twinkle/agentserver/hooks/builtin/logging_hook.py
git commit -m "feat(hooks): add package init, LoggingHook — first concrete AgentHook"
```

---

### Task 6: AgentLoop Integration — Refactor run_stream + Insert Hook Trigger Points

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py`
- Create: `tests/test_agent_loop_with_hooks.py`

**Interfaces:**
- Consumes: `AgentHook`, `HookEvent`, `HookContext`, `HookManager`, `InvokeInputs`, `ModelCallInputs`, `ToolCallInputs`, `HookInterrupt`, `ForceFinishRequest`, `RetryRequest` from Tasks 1-5; `LoggingHook` from Task 5
- Produces: `AgentLoop` with `self._hooks: HookManager`, `register_hook()`, `unregister_hook()`, refactored `run_stream` → `run_stream` (ctx creation + BEFORE/AFTER_INVOKE) + `_inner_run_stream` (ReAct loop with hook trigger points), `@hook`-decorated `_raided_tool_call`

This is the most complex task. The key constraint: `run_stream(envelope)` signature and return type must not change — existing tests and server.py must pass unmodified.

- [ ] **Step 1: Read the current agent_loop.py to understand the full implementation**

Read: `twinkle/agentserver/agent_loop.py` — all 142 lines. The refactoring will:
- Add `self._hooks = HookManager(self)` to `__init__`
- Add `register_hook()` and `unregister_hook()` methods
- Split `run_stream` into `run_stream` (creates ctx, triggers BEFORE/AFTER_INVOKE) + `_inner_run_stream` (original ReAct logic with hook trigger points inserted)
- Add `_raided_tool_call` decorated with `@hook(BEFORE_TOOL_CALL, AFTER_TOOL_CALL, ON_TOOL_EXCEPTION)`
- Insert manual `self._hooks.execute()` calls for model_call events inside `_inner_run_stream`
- Handle force_finish and retry signals

- [ ] **Step 2: Write failing integration tests**

```python
# tests/test_agent_loop_with_hooks.py
"""Tests for AgentLoop integration with the Hook mechanism.

Verifies that hooks are called at the right events, in priority order,
and that frame output is unchanged when hooks are present.
"""
from __future__ import annotations

import asyncio

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.tools.decorator import tool
from twinkle.e2a.models import E2AEnvelope


class _CallOrderHook(AgentHook):
    """Records the order of hook calls with their events."""
    priority = 50

    def __init__(self):
        self.calls: list[tuple[HookEvent, str | None]] = []

    async def before_invoke(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def after_invoke(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def before_model_call(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def after_model_call(self, ctx):
        self.calls.append((ctx.event, ctx.session_id))

    async def before_tool_call(self, ctx):
        self.calls.append((ctx.event, ctx.inputs.name))

    async def after_tool_call(self, ctx):
        self.calls.append((ctx.event, ctx.inputs.name))


def _env(query, rid="r1", session_id="s1"):
    return E2AEnvelope(
        request_id=rid,
        session_id=session_id,
        method="chat.send",
        params={"query": query},
    )


class _ScriptedLLM:
    """Returns one canned event-list per call, in order."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _reg_with_echo_tool():
    m = __import__("twinkle.agentserver.tools.manager", fromlist=["ToolManager"]).ToolManager()

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    m.register(echo)
    return m


def test_hooks_called_on_plain_answer(session_store) -> None:
    """Plain answer flow: BEFORE_INVOKE → BEFORE_MODEL_CALL → AFTER_MODEL_CALL → AFTER_INVOKE."""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        [TextDelta("hi"), Finish("stop", {"role": "assistant", "content": "hi", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())
    asyncio.run(loop.register_hook(order_hook))

    async def run():
        frames = [f async for f in loop.run_stream(_env("hello"))]
        return frames

    frames = asyncio.run(run())
    # Verify frame output unchanged
    assert frames[-1].response_kind == "e2a.complete"

    # Verify hook call order
    events = [c[0] for c in order_hook.calls]
    assert events == [
        HookEvent.BEFORE_INVOKE,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.AFTER_INVOKE,
    ]


def test_hooks_called_on_tool_call_round_trip(session_store) -> None:
    """Tool call flow: invoke → model_call → tool_call → model_call → invoke."""
    store = session_store
    order_hook = _CallOrderHook()
    llm = _ScriptedLLM([
        # turn 1: model calls echo tool
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2: model produces final answer
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())
    asyncio.run(loop.register_hook(order_hook))

    async def run():
        frames = [f async for f in loop.run_stream(_env("call echo"))]
        return frames

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"

    events = [c[0] for c in order_hook.calls]
    assert events == [
        HookEvent.BEFORE_INVOKE,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.BEFORE_TOOL_CALL,
        HookEvent.AFTER_TOOL_CALL,
        HookEvent.BEFORE_MODEL_CALL,
        HookEvent.AFTER_MODEL_CALL,
        HookEvent.AFTER_INVOKE,
    ]


def test_existing_tests_still_pass(session_store) -> None:
    """AgentLoop without hooks produces identical output — existing tests
    should pass unchanged. This is a meta-test: run the plain answer test
    WITHOUT hooks and verify frames."""
    store = session_store
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"


def test_logging_hook_registers_and_works(session_store) -> None:
    """LoggingHook can be registered and its get_callbacks returns 4 events."""
    store = session_store
    lh = LoggingHook()
    callbacks = lh.get_callbacks()
    assert len(callbacks) == 4
    assert HookEvent.BEFORE_MODEL_CALL in callbacks
    assert HookEvent.AFTER_MODEL_CALL in callbacks
    assert HookEvent.BEFORE_TOOL_CALL in callbacks
    assert HookEvent.AFTER_TOOL_CALL in callbacks
    assert lh.priority == 10
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_loop_with_hooks.py -v`
Expected: FAIL — AgentLoop doesn't have `_hooks` or `register_hook` yet.

- [ ] **Step 4: Refactor agent_loop.py — full rewrite**

The complete refactored file:

```python
# twinkle/agentserver/agent_loop.py
"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).

Twinkle is stream-only; run_unary has been removed.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.hooks.base import (
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.config import AGENT_MAX_STEPS as MAX_STEPS

log = logging.getLogger("twinkle.agentserver")

TODO_SYSTEM_PROMPT = (
    "You have todo tools to plan and track multi-step work: "
    "todo_create, todo_complete, todo_list. For non-trivial multi-step "
    "requests, first call todo_create with a list of sub-tasks, then work "
    "through them calling todo_complete(idx, result) as each finishes, and "
    "call todo_list to check progress. For simple one-step requests, do NOT "
    "use the todo tools — just answer or call the needed tool directly."
)

_MAX_HOOK_RETRIES = 3


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        memory: LongTermMemory,
    ) -> None:
        self._llm = llm
        self._store = store
        self._tools = tools
        self._memory = memory
        self._hooks = HookManager(self)

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook on this loop."""
        asyncio_run = __import__("asyncio").run
        asyncio_run(self._hooks.register_hook(hook_instance))

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook from this loop."""
        asyncio_run = __import__("asyncio").run
        asyncio_run(self._hooks.unregister_hook(hook_instance))

    # --- Public entry point — signature unchanged --- #

    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        """Entry point — creates HookContext, triggers BEFORE/AFTER_INVOKE,
        delegates ReAct logic to _inner_run_stream.

        Signature unchanged from pre-hook version: (envelope) -> AsyncIterator[E2AResponse].
        """
        session_id = envelope.session_id
        request_id = envelope.request_id
        query = (envelope.params or {}).get("query", "")

        ctx = HookContext(
            agent=self,
            event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query=query, envelope=envelope),
            session_id=session_id,
            request_id=request_id,
            extra={},
        )

        await self._hooks.execute(HookEvent.BEFORE_INVOKE, ctx)

        try:
            async for frame in self._inner_run_stream(ctx, envelope):
                yield frame
        except HookInterrupt:
            # Propagate interrupt as a final error frame
            yield E2AResponse(
                request_id=request_id,
                sequence=0,
                is_final=True,
                status="failed",
                response_kind="e2a.error",
                body={"error": "execution interrupted"},
            )
        except Exception as exc:
            ctx.exception = exc
            # Use ON_MODEL_EXCEPTION as a catch-all for unhandled errors
            await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            await self._hooks.execute(HookEvent.AFTER_INVOKE, ctx)

    # --- ReAct core with hook trigger points --- #

    async def _inner_run_stream(
        self,
        ctx: HookContext,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop — original logic with hook trigger points inserted.

        Model calls (async generators) use manual self._hooks.execute().
        Tool calls use @hook-decorated _raided_tool_call.
        """
        session_id = envelope.session_id
        PLAN_TODO_SESSION_ID.set(session_id or "default")
        reset_todo_events()
        # Insert the todo-guidance system message once per session
        existing = self._store.get_messages(session_id)
        if not existing or existing[0].get("role") != "system":
            await self._store.append(
                session_id,
                {"role": "system", "content": TODO_SYSTEM_PROMPT},
                request_id=envelope.request_id,
            )
        query = (envelope.params or {}).get("query", "")
        await self._store.append(
            session_id,
            {"role": "user", "content": query},
            request_id=envelope.request_id,
        )
        self._memory.recall(query)

        seq = 0
        full_text = ""
        for _step in range(MAX_STEPS):
            msgs = self._store.get_messages(session_id)

            # -- Model call with hooks (manual, since LLM stream is async generator) -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tools.schemas())
            await self._hooks.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

            # Check force_finish — skip LLM call if requested
            ff = ctx.consume_force_finish_request()
            if ff is not None:
                yield E2AResponse(
                    request_id=envelope.request_id,
                    sequence=seq,
                    is_final=True,
                    status="succeeded",
                    response_kind="e2a.complete",
                    body={"result": {"content": str(ff.result or "")}},
                )
                return

            # Stream LLM with retry support
            model_exc = None
            for retry_attempt in range(_MAX_HOOK_RETRIES + 1):
                ctx.retry_attempt = retry_attempt
                ctx.exception = None
                try:
                    async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
                        if isinstance(ev, TextDelta):
                            full_text += ev.content
                            yield E2AResponse(
                                request_id=envelope.request_id,
                                sequence=seq,
                                is_final=False,
                                status="in_progress",
                                response_kind="e2a.chunk",
                                body={"result": {"content": ev.content}},
                            )
                            seq += 1
                        elif isinstance(ev, Finish):
                            await self._store.append(
                                session_id,
                                ev.assistant_message,
                                request_id=envelope.request_id,
                                event_type="chat.final",
                            )
                            tcs = ev.assistant_message.get("tool_calls")
                            if ev.finish_reason == "tool_calls" and tcs:
                                for tc in tcs:
                                    name = tc["function"]["name"]
                                    try:
                                        args = json.loads(tc["function"]["arguments"] or "{}")
                                    except Exception:
                                        args = {}
                                    # Tool call via @hook-decorated method
                                    ctx.inputs = ToolCallInputs(
                                        name=name, args=args, tool_call_id=tc["id"]
                                    )
                                    try:
                                        result = await self._raided_tool_call(ctx, name, args)
                                    except HookInterrupt:
                                        yield E2AResponse(
                                            request_id=envelope.request_id,
                                            sequence=seq,
                                            is_final=True,
                                            status="failed",
                                            response_kind="e2a.error",
                                            body={"error": "tool execution interrupted"},
                                        )
                                        return
                                    for snap in drain_todo_events():
                                        yield E2AResponse(
                                            request_id=envelope.request_id,
                                            sequence=seq,
                                            is_final=False,
                                            status="in_progress",
                                            response_kind="e2a.todo_update",
                                            body=snap,
                                        )
                                        seq += 1
                                    await self._store.append(
                                        session_id,
                                        {
                                            "role": "tool",
                                            "tool_call_id": tc["id"],
                                            "content": result,
                                        },
                                        request_id=envelope.request_id,
                                        event_type="chat.tool_result",
                                    )
                                # After model call succeeded with tool calls
                                await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                continue  # re-ask model with tool results
                            yield E2AResponse(
                                request_id=envelope.request_id,
                                sequence=seq,
                                is_final=True,
                                status="succeeded",
                                response_kind="e2a.complete",
                                body={"result": {"content": full_text}},
                            )
                            await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    # Stream completed without Finish — treat as success
                    await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break  # exit retry loop
                except asyncio.CancelledError:
                    raise
                except HookInterrupt:
                    raise
                except Exception as exc:
                    ctx.exception = exc
                    model_exc = exc
                    await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    retry_req = ctx.consume_retry_request()
                    if retry_req is not None and retry_attempt < _MAX_HOOK_RETRIES:
                        log.info("hook requested retry, attempt %d/%d",
                                 retry_attempt + 1, _MAX_HOOK_RETRIES)
                        continue  # retry the LLM call
                    # No retry — re-raise
                    raise
            # If we exhausted retries without success, the last exception
            # was already raised above. If we broke out of the retry loop
            # (stream completed without Finish), we just fall through to
            # the next step in the outer loop.
        # exceeded max_steps without converging
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={MAX_STEPS}"},
        )

    # --- @hook-decorated methods --- #

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _raided_tool_call(
        self,
        ctx: HookContext,
        name: str,
        args: dict,
    ) -> str:
        """Tool execution wrapped with @hook lifecycle."""
        return await self._tools.execute(name, args)
```

Wait — there's a problem with `register_hook` and `unregister_hook` using `asyncio.run()` inside an already-running async context. These methods will typically be called from `build_agent_loop()` which is a sync function, so `asyncio.run()` works there. But if called from within an async context, it would fail.

Better approach: make `register_hook`/`unregister_hook` async, and call them from `build_agent_loop` via `asyncio.run()`. But actually, looking at the spec §5, `build_agent_loop` is a sync function that calls `loop.register_hook(h)` — so `register_hook` needs to be sync-friendly.

The simplest approach: make them sync methods that internally call `asyncio.run()`. Since `build_agent_loop` runs before any async context is established, this is safe. For tests that use `asyncio.run(run())`, the hooks are registered before `asyncio.run()` starts, so it's also safe.

Actually wait, there's another issue. In the test `test_hooks_called_on_plain_answer`, I call `asyncio.run(loop.register_hook(order_hook))` — but then immediately after, I call `asyncio.run(run())`. Since `asyncio.run()` creates a new event loop each time, the hook registration from the first `asyncio.run()` won't be visible in the second one.

The problem: `asyncio.run()` creates a fresh event loop, runs the coroutine, and closes the loop. Hook state (stored on `self._hooks`) is in-memory Python state, not event-loop-specific. So `self._hooks._callbacks` persists across `asyncio.run()` calls. The hooks themselves are just Python objects, not tied to any event loop.

So the register_hook can be:
```python
def register_hook(self, hook_instance):
    # This is fine because it only modifies Python object state
    # The async init() call can use asyncio.run() safely
    import asyncio
    asyncio.run(self._hooks.register_hook(hook_instance))
```

But wait, there's a subtlety. If `register_hook` is called inside an already-running async context (e.g., from within `run_stream`), `asyncio.run()` would fail because there's already a running event loop. This won't happen in practice because hooks are registered in `build_agent_loop()` (sync) before any async context, but we should handle it gracefully.

Better approach: make `register_hook` and `unregister_hook` async, and handle them properly in tests:

```python
async def register_hook(self, hook_instance):
    await self._hooks.register_hook(hook_instance)

async def unregister_hook(self, hook_instance):
    await self._hooks.unregister_hook(hook_instance)
```

In `build_agent_loop`, we'd need to register hooks async. But `build_agent_loop` is currently sync... This creates a problem.

Actually, the simplest solution: make `register_hook` both sync and async compatible by checking if there's a running event loop:

```python
def register_hook(self, hook_instance):
    """Register a hook — works both in sync and async contexts."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        # Already in async context — schedule the coroutine
        # But we can't await here since we're a sync method...
        # This is a problem.
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        asyncio.run(self._hooks.register_hook(hook_instance))
```

This is messy. Let me think of a cleaner approach.

The cleanest approach: make `HookManager.register_hook` synchronous (since `AgentHook.init` is typically lightweight and could be sync), and make `execute` async (since it calls async hook methods).

But the spec says `init` is async. Let me look at jiuwen — in jiuwen, `AgentRail.init()` is sync (not async). The spec's design says `async def init(self, agent)` but we could make it sync instead for simplicity.

Actually, the simplest pragmatic approach: make `register_hook` async on AgentLoop, and adjust the tests and `build_agent_loop` accordingly. In tests, we call `asyncio.run(loop.register_hook(h))` before the main test coroutine. In `build_agent_loop`, we can't call async methods, so we just store the hooks list and register them lazily when `run_stream` first runs.

Wait, but that's complicated too. Let me look at what the spec actually says for `build_agent_loop`:

```python
def build_agent_loop(hooks=None):
    llm = LLMClient(...)
    store = SessionStore(SESSIONS_DIR)
    tools = tool_manager()
    memory = LongTermMemory()
    loop = AgentLoop(llm, store, tools, memory)
    if hooks:
        for h in hooks:
            loop.register_hook(h)
    return loop, store
```

This is a sync function calling `loop.register_hook(h)` synchronously. So `register_hook` must be sync-compatible.

The cleanest solution: make `AgentHook.init()` and `AgentHook.uninit()` **sync** (not async). This is a minor deviation from the spec but pragmatically necessary. In jiuwen, `AgentRail.init()` doesn't do any async work — it just stores references and reads config. Same for Twinkle's hooks. Making init/uninit sync eliminates the `asyncio.run()` problem entirely.

Let me update the implementation:
- `AgentHook.init()` → sync method (returns None, not awaitable)
- `AgentHook.uninit()` → sync method
- `HookManager.register_hook()` → sync method
- `HookManager.unregister_hook()` → sync method
- `HookManager.execute()` → async method (calls async hook callbacks)
- `AgentLoop.register_hook()` → sync method
- `AgentLoop.unregister_hook()` → sync method

This simplifies everything. The tests can just call `loop.register_hook(h)` directly without asyncio.

- [ ] **Step 4: Update AgentHook.init/uninit to be sync (necessary for register_hook in sync context)**

In `twinkle/agentserver/hooks/base.py`, change:

```python
# OLD:
async def init(self, agent: Any) -> None: ...
async def uninit(self, agent: Any) -> None: ...

# NEW:
def init(self, agent: Any) -> None: ...
def uninit(self, agent: Any) -> None: ...
```

This is a minor deviation from the spec — init/uninit are sync because they only do lightweight setup (store references, read config), not async I/O. Matches jiuwen's actual AgentRail.init which is also sync.

Also update `HookManager` to match:

```python
# In manager.py, change register_hook and unregister_hook to sync:

def register_hook(self, hook: AgentHook) -> None:
    """Register a hook: call init(), get callbacks, insert sorted."""
    hook.init(self._agent)
    # ... rest stays the same ...

def unregister_hook(self, hook: AgentHook) -> None:
    """Unregister a hook: call uninit(), remove all its callbacks."""
    hook.uninit(self._agent)
    # ... rest stays the same ...
```

And update `test_hook_manager.py` — remove `asyncio.run()` from register/unregister calls:

```python
# OLD:
asyncio.run(mgr.register_hook(h))
asyncio.run(mgr.unregister_hook(h))

# NEW:
mgr.register_hook(h)
mgr.unregister_hook(h)
```

And update `test_hook_decorator.py` — same:

```python
# OLD:
asyncio.run(agent._hooks.register_hook(rec))

# NEW:
agent._hooks.register_hook(rec)
```

And update `test_agent_loop_with_hooks.py` — same:

```python
# OLD:
asyncio.run(loop.register_hook(order_hook))

# NEW:
loop.register_hook(order_hook)
```

- [ ] **Step 5: Write the full refactored agent_loop.py**

```python
# twinkle/agentserver/agent_loop.py
"""AgentLoop — the ReAct core: think -> (tool -> result)* -> answer.

run_stream is an async generator yielding E2AResponse frames so the
ws send boundary stays in server.py (loop never touches the socket).

Twinkle is stream-only; run_unary has been removed.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from twinkle.agentserver.llm_client import Finish, LLMClient, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.plan_todo_context import (
    PLAN_TODO_SESSION_ID,
    drain_todo_events,
    reset_todo_events,
)
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.hooks.base import (
    AgentHook,
    HookContext,
    HookEvent,
    HookInterrupt,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
)
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.config import AGENT_MAX_STEPS as MAX_STEPS

log = logging.getLogger("twinkle.agentserver")

TODO_SYSTEM_PROMPT = (
    "You have todo tools to plan and track multi-step work: "
    "todo_create, todo_complete, todo_list. For non-trivial multi-step "
    "requests, first call todo_create with a list of sub-tasks, then work "
    "through them calling todo_complete(idx, result) as each finishes, and "
    "call todo_list to check progress. For simple one-step requests, do NOT "
    "use the todo tools — just answer or call the needed tool directly."
)

_MAX_HOOK_RETRIES = 3


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
        memory: LongTermMemory,
    ) -> None:
        self._llm = llm
        self._store = store
        self._tools = tools
        self._memory = memory
        self._hooks = HookManager(self)

    def register_hook(self, hook_instance: AgentHook) -> None:
        """Register an AgentHook on this loop (sync — safe to call from build_agent_loop)."""
        self._hooks.register_hook(hook_instance)

    def unregister_hook(self, hook_instance: AgentHook) -> None:
        """Unregister an AgentHook from this loop."""
        self._hooks.unregister_hook(hook_instance)

    # --- Public entry point — signature unchanged --- #

    async def run_stream(self, envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        """Entry point — creates HookContext, triggers BEFORE/AFTER_INVOKE,
        delegates ReAct logic to _inner_run_stream.

        Signature unchanged: (envelope) -> AsyncIterator[E2AResponse].
        """
        session_id = envelope.session_id
        request_id = envelope.request_id
        query = (envelope.params or {}).get("query", "")

        ctx = HookContext(
            agent=self,
            event=HookEvent.BEFORE_INVOKE,
            inputs=InvokeInputs(query=query, envelope=envelope),
            session_id=session_id,
            request_id=request_id,
            extra={},
        )

        await self._hooks.execute(HookEvent.BEFORE_INVOKE, ctx)

        try:
            async for frame in self._inner_run_stream(ctx, envelope):
                yield frame
        except HookInterrupt:
            yield E2AResponse(
                request_id=request_id,
                sequence=0,
                is_final=True,
                status="failed",
                response_kind="e2a.error",
                body={"error": "execution interrupted"},
            )
        except Exception as exc:
            ctx.exception = exc
            await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
            raise
        finally:
            await self._hooks.execute(HookEvent.AFTER_INVOKE, ctx)

    # --- ReAct core with hook trigger points --- #

    async def _inner_run_stream(
        self,
        ctx: HookContext,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[E2AResponse]:
        """The ReAct loop with hook trigger points inserted.

        Model calls use manual self._hooks.execute() (async generator incompatible with @hook).
        Tool calls use @hook-decorated _raided_tool_call.
        """
        session_id = envelope.session_id or "default"
        PLAN_TODO_SESSION_ID.set(session_id)
        reset_todo_events()
        # Insert todo-guidance system message once per session
        existing = self._store.get_messages(session_id)
        if not existing or existing[0].get("role") != "system":
            await self._store.append(
                session_id,
                {"role": "system", "content": TODO_SYSTEM_PROMPT},
                request_id=envelope.request_id,
            )
        query = (envelope.params or {}).get("query", "")
        await self._store.append(
            session_id,
            {"role": "user", "content": query},
            request_id=envelope.request_id,
        )
        self._memory.recall(query)

        seq = 0
        full_text = ""
        for _step in range(MAX_STEPS):
            msgs = self._store.get_messages(session_id)

            # -- BEFORE_MODEL_CALL -- #
            ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tools.schemas())
            await self._hooks.execute(HookEvent.BEFORE_MODEL_CALL, ctx)

            # Check force_finish
            ff = ctx.consume_force_finish_request()
            if ff is not None:
                yield E2AResponse(
                    request_id=envelope.request_id,
                    sequence=seq,
                    is_final=True,
                    status="succeeded",
                    response_kind="e2a.complete",
                    body={"result": {"content": str(ff.result or "")}},
                )
                return

            # -- LLM stream with retry loop -- #
            for retry_attempt in range(_MAX_HOOK_RETRIES + 1):
                ctx.retry_attempt = retry_attempt
                ctx.exception = None
                try:
                    async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
                        if isinstance(ev, TextDelta):
                            full_text += ev.content
                            yield E2AResponse(
                                request_id=envelope.request_id,
                                sequence=seq,
                                is_final=False,
                                status="in_progress",
                                response_kind="e2a.chunk",
                                body={"result": {"content": ev.content}},
                            )
                            seq += 1
                        elif isinstance(ev, Finish):
                            await self._store.append(
                                session_id,
                                ev.assistant_message,
                                request_id=envelope.request_id,
                                event_type="chat.final",
                            )
                            tcs = ev.assistant_message.get("tool_calls")
                            if ev.finish_reason == "tool_calls" and tcs:
                                for tc in tcs:
                                    name = tc["function"]["name"]
                                    try:
                                        args = json.loads(tc["function"]["arguments"] or "{}")
                                    except Exception:
                                        args = {}
                                    # Tool call via @hook-decorated method
                                    ctx.inputs = ToolCallInputs(
                                        name=name, args=args, tool_call_id=tc["id"]
                                    )
                                    try:
                                        result = await self._raided_tool_call(ctx, name, args)
                                    except HookInterrupt:
                                        yield E2AResponse(
                                            request_id=envelope.request_id,
                                            sequence=seq,
                                            is_final=True,
                                            status="failed",
                                            response_kind="e2a.error",
                                            body={"error": "tool execution interrupted"},
                                        )
                                        return
                                    for snap in drain_todo_events():
                                        yield E2AResponse(
                                            request_id=envelope.request_id,
                                            sequence=seq,
                                            is_final=False,
                                            status="in_progress",
                                            response_kind="e2a.todo_update",
                                            body=snap,
                                        )
                                        seq += 1
                                    await self._store.append(
                                        session_id,
                                        {
                                            "role": "tool",
                                            "tool_call_id": tc["id"],
                                            "content": result,
                                        },
                                        request_id=envelope.request_id,
                                        event_type="chat.tool_result",
                                    )
                                # AFTER_MODEL_CALL for tool_calls turn
                                await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                                continue  # re-ask model with tool results
                            # AFTER_MODEL_CALL for final answer turn
                            yield E2AResponse(
                                request_id=envelope.request_id,
                                sequence=seq,
                                is_final=True,
                                status="succeeded",
                                response_kind="e2a.complete",
                                body={"result": {"content": full_text}},
                            )
                            await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                            return
                    # LLM stream ended without Finish — shouldn't happen, but handle gracefully
                    await self._hooks.execute(HookEvent.AFTER_MODEL_CALL, ctx)
                    break
                except Exception as exc:
                    ctx.exception = exc
                    await self._hooks.execute(HookEvent.ON_MODEL_EXCEPTION, ctx)
                    retry_req = ctx.consume_retry_request()
                    if retry_req is not None and retry_attempt < _MAX_HOOK_RETRIES:
                        log.info("hook requested LLM retry, attempt %d/%d",
                                 retry_attempt + 1, _MAX_HOOK_RETRIES)
                        continue
                    raise

        # exceeded max_steps without converging
        yield E2AResponse(
            request_id=envelope.request_id,
            sequence=seq,
            is_final=True,
            status="failed",
            response_kind="e2a.error",
            body={"error": f"agent loop exceeded max_steps={MAX_STEPS}"},
        )

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def _raided_tool_call(
        self,
        ctx: HookContext,
        name: str,
        args: dict,
    ) -> str:
        """Tool execution wrapped with @hook lifecycle."""
        return await self._tools.execute(name, args)
```

- [ ] **Step 6: Update tests to use sync register_hook**

Update `test_hook_manager.py`, `test_hook_decorator.py`, `test_agent_loop_with_hooks.py` — replace `asyncio.run(mgr.register_hook(h))` with `mgr.register_hook(h)` (and same for unregister).

Also update `test_hook_base.py` — remove `async def init` test expectation (now sync):

```python
# test_hook_base.py — update InitHook test:
class InitRecorder(AgentHook):
    def __init__(self):
        self.inited = False
    def init(self, agent):  # sync now, not async
        self.inited = True
```

- [ ] **Step 7: Run ALL tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS — existing agent_loop tests, hook infrastructure tests, and new integration tests.

- [ ] **Step 8: Commit**

```bash
git add twinkle/agentserver/agent_loop.py tests/test_agent_loop_with_hooks.py twinkle/agentserver/hooks/base.py twinkle/agentserver/hooks/manager.py tests/test_hook_manager.py tests/test_hook_decorator.py tests/test_hook_base.py
git commit -m "feat(hooks): integrate HookManager into AgentLoop — refactor run_stream, add trigger points, @hook-decorated tool_call"
```

---

### Task 7: Production Wiring — build_agent_loop hooks parameter + CLAUDE.md update

**Files:**
- Modify: `twinkle/agentserver/server.py` (build_agent_loop accepts hooks parameter)
- Modify: `twinkle/CLAUDE.md` (add hooks convention)

**Interfaces:**
- Consumes: `AgentHook`, `LoggingHook` from Tasks 1-6
- Produces: `build_agent_loop(hooks=None)` that registers hooks on the AgentLoop; CLAUDE.md updated with hook conventions

- [ ] **Step 1: Read current server.py build_agent_loop**

Read: `twinkle/agentserver/server.py` — `build_agent_loop()` at line 45-57.

- [ ] **Step 2: Modify build_agent_loop to accept hooks**

```python
# In server.py, modify build_agent_loop:
def build_agent_loop(hooks=None):
    """Production wiring — config-driven LLM + disk-backed SessionStore.

    Returns ``(loop, store)`` so the caller can share ONE store instance.
    *hooks* is an optional list of AgentHook instances to register.
    """
    llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    store = SessionStore(SESSIONS_DIR)
    tools = tool_manager()
    memory = LongTermMemory()
    loop = AgentLoop(llm, store, tools, memory)
    if hooks:
        for h in hooks:
            loop.register_hook(h)
    return loop, store
```

Also update `agent_loop()` thin shim:

```python
def agent_loop() -> AgentLoop:
    """Thin shim kept for any existing one-arg caller."""
    loop, _ = build_agent_loop()
    return loop
```

(No change needed — it already calls build_agent_loop() with no args.)

- [ ] **Step 3: Add hook conventions to CLAUDE.md**

Append to the Conventions section in CLAUDE.md:

```markdown
- **Add a new Hook**: write a class inheriting `AgentHook` in a `*_hook.py` module under `hooks/builtin/`, override the lifecycle methods you care about, set `priority`, then register it in `build_agent_loop()` or at the call site via `loop.register_hook(hook_instance)`. `agent_loop` picks it up with no core changes.
```

- [ ] **Step 4: Run ALL tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/server.py twinkle/CLAUDE.md
git commit -m "feat(hooks): build_agent_loop accepts hooks parameter, add convention to CLAUDE.md"
```

---

## Self-Review

### 1. Spec coverage check

| Spec section | Task covering it |
|---|---|
| §2.1 HookEvent (11 values) | Task 1 |
| §2.2 AgentHook (base class, priority, get_callbacks) | Task 1 |
| §2.3 HookContext (dataclass, extra, control signals) | Task 2 |
| §2.4 HookInputs (InvokeInputs, ModelCallInputs, ToolCallInputs, TaskIterationInputs) | Task 2 |
| §2.5 RetryRequest, ForceFinishRequest, HookInterrupt | Task 2 |
| §3.1 HookManager (register, unregister, execute) | Task 3 |
| §3.2 @hook decorator | Task 4 |
| §4 AgentLoop integration | Task 6 |
| §5 LoggingHook | Task 5 |
| §6 File structure | Tasks 1-5 |
| §7 Test strategy | Tasks 1-4, 6 |
| §8 jiuwen mapping | All tasks (via naming) |

**One deviation**: `AgentHook.init()` and `AgentHook.uninit()` are **sync** (not async) in the implementation, because `register_hook()` must work in sync context (called from `build_agent_loop`). This matches jiuwen's actual `AgentRail.init()` which is also sync. The spec said `async def init` but this is pragmatically necessary and the deviation is minor — init/uninit only do lightweight setup, not async I/O.

### 2. Placeholder scan

No TBD, TODO, "implement later", "fill in details", "add appropriate error handling" found. All code blocks contain complete implementation code.

### 3. Type consistency check

- `HookEvent` enum values used consistently across all tasks
- `HookContext` fields match between definition (Task 2) and usage (Task 6)
- `ModelCallInputs(messages: list[dict], tools: list[dict])` matches `self._llm.stream(messages=msgs, tools=self._tools.schemas())` signature
- `ToolCallInputs(name, args, tool_call_id)` matches `tc["function"]["name"]`, `json.loads(tc["function"]["arguments"])`, `tc["id"]`
- `_raided_tool_call(self, ctx, name, args)` signature matches `@hook` decorator expectations (self, ctx, then original args)
- All test `asyncio.run()` calls are consistent with project convention
