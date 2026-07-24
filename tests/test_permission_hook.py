import asyncio

from twinkle.agentserver.hooks.base import (
    AgentHook, HookContext, HookEvent, ToolCallInputs, HookInterrupt)
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.agentserver.permission_context import (
    APPROVAL_CHANNEL, set_permission_channel)
from twinkle.agentserver.permissions.models import PermissionDecision, PermissionLevel


class _FakeEngine:
    def __init__(self, level):
        self._level = level
        self.checked = []
    def check(self, tool, args, channel, session_id, request_id):
        self.checked.append((tool, channel))
        if self._level == "deny":
            return PermissionDecision(level="deny", source="rule", rule_id="x",
                                      deny_message="[ERROR] denied")
        if self._level == "ask":
            return PermissionDecision(level="ask", source="tier", reason="require-approval")
        return PermissionDecision(level="allow", source="tier")


def _ctx(tc_id="c1"):
    return HookContext(agent=None, event=HookEvent.BEFORE_TOOL_CALL,
                       inputs=ToolCallInputs(name="echo", args={"text": "hi"}, tool_call_id=tc_id),
                       session_id="s", request_id="r", extra={})


def test_allow_is_noop():
    e = _FakeEngine("allow")
    asyncio.run(PermissionHook(e).before_tool_call(_ctx()))
    assert e.checked[0] == ("echo", "default")  # ContextVar default


def test_deny_sets_force_finish():
    e = _FakeEngine("deny")
    ctx = _ctx()
    asyncio.run(PermissionHook(e).before_tool_call(ctx))
    ff = ctx.consume_force_finish_request()
    assert ff is not None and ff.result == "[ERROR] denied"


def test_ask_raises_hookinterrupt_with_payload():
    e = _FakeEngine("ask")
    tok = set_permission_channel("web")
    try:
        try:
            asyncio.run(PermissionHook(e).before_tool_call(_ctx("c9")))
            raised = False
        except HookInterrupt as hi:
            raised = True
            assert hi.data["tool"] == "echo"
            assert hi.data["tool_call_id"] == "c9"
            assert hi.data["approval_id"]  # uuid present
            assert hi.data["reason"] == "require-approval"
        assert raised
    finally:
        APPROVAL_CHANNEL.reset(tok)  # Python 3.14: ContextVar.reset(token), not token.reset()


def test_approved_bypass_skips_check():
    e = _FakeEngine("ask")  # would ask, but bypass should skip
    ctx = _ctx("c1")
    ctx.extra["_approved_tool_call_ids"] = {"c1"}
    asyncio.run(PermissionHook(e).before_tool_call(ctx))
    assert e.checked == []  # engine never called
