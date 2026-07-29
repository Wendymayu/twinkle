"""Tests for SubagentContextHook — sets the subagent ContextVar bridge.

The hook holds the executor (passed at construction, auto-wired by
build_agent_loop). ContextVar .set() inside asyncio.run() does NOT propagate
to the outer context, so assertions run inside the same coroutine that awaits
before_invoke.
"""
import asyncio

from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.tools.builtin.subagent.context import (
    get_subagent_executor,
    get_subagent_parent_session_id,
    get_subagent_parent_request_id,
)


def _ctx(session_id="s1", request_id="r1"):
    return HookContext(
        agent=None,
        event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q", envelope=None),
        session_id=session_id,
        request_id=request_id,
        extra={},
    )


def test_before_invoke_sets_contextvars():
    from twinkle.agentserver.hooks.builtin.subagent_context_hook import SubagentContextHook

    sentinel = object()
    hook = SubagentContextHook(executor=sentinel)

    async def run():
        await hook.before_invoke(_ctx("s9", "r9"))
        assert get_subagent_executor() is sentinel
        assert get_subagent_parent_session_id() == "s9"
        assert get_subagent_parent_request_id() == "r9"

    asyncio.run(run())


def test_priority():
    from twinkle.agentserver.hooks.builtin.subagent_context_hook import SubagentContextHook

    assert SubagentContextHook.priority == 50
