"""Task 10 — HookManager.execute() must propagate HookInterrupt.

A HookInterrupt is a HITL control-flow signal (e.g., a permission hook
asking for approval). It must reach the AgentLoop caller, NOT be caught
by execute()'s fail-soft `except Exception` (which would log + swallow it).
All other exceptions keep fail-soft behavior (logged, continue).
"""
import asyncio

from twinkle.agentserver.hooks.base import AgentHook, HookContext, HookEvent, HookInterrupt


class _RaisingHook(AgentHook):
    async def before_tool_call(self, ctx):
        raise HookInterrupt(message="approval", data={"approval_id": "a1"})


class _ExplodingHook(AgentHook):
    async def before_tool_call(self, ctx):
        raise RuntimeError("boom")


def test_hookinterrupt_propagates_not_swallowed():
    class _Agent: ...
    from twinkle.agentserver.hooks.manager import HookManager
    hm = HookManager(_Agent())
    hm.register_hook(_RaisingHook())
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_TOOL_CALL, inputs=None,
                      session_id="s", request_id="r", extra={})
    try:
        asyncio.run(hm.execute(HookEvent.BEFORE_TOOL_CALL, ctx))
        raised = False
    except HookInterrupt as hi:
        raised = True
        assert hi.data["approval_id"] == "a1"
    assert raised, "HookInterrupt must propagate, not be swallowed"


def test_other_exceptions_still_fail_soft():
    class _Agent: ...
    from twinkle.agentserver.hooks.manager import HookManager
    hm = HookManager(_Agent())
    hm.register_hook(_ExplodingHook())
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_TOOL_CALL, inputs=None,
                      session_id="s", request_id="r", extra={})
    asyncio.run(hm.execute(HookEvent.BEFORE_TOOL_CALL, ctx))  # no raise = fail-soft preserved
