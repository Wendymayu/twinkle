"""Task 10 — HookManager.execute() 必须向上传播 HookInterrupt。

HookInterrupt 是 HITL 控制流信号(例如权限 hook 请求审批)。
它必须到达 AgentLoop 调用方,不能被 execute() 的 fail-soft
`except Exception` 捕获(那会 log + 吞掉)。
其他异常保持 fail-soft 行为(log 后继续)。
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
    hm = HookManager()
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
    hm = HookManager()
    hm.register_hook(_ExplodingHook())
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_TOOL_CALL, inputs=None,
                      session_id="s", request_id="r", extra={})
    asyncio.run(hm.execute(HookEvent.BEFORE_TOOL_CALL, ctx))  # no raise = fail-soft preserved
