"""SubagentContextHook 的测试——设置 subagent 的 ContextVar 桥接。

该 hook 持有 executor（构造时传入，由 create_agent 自动装配）。在
asyncio.run() 内部 ContextVar 的 .set() 不会传播到外层 context，因此
断言运行在与 await before_invoke 相同的协程内。
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
