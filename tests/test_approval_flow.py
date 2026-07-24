import asyncio

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY
from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.engine import PermissionEngine
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.e2a.models import E2AEnvelope


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts; self.calls = 0
    async def stream(self, messages, tools):
        evs = self._scripts[self.calls]; self.calls += 1
        for ev in evs:
            yield ev


def _env(query, rid="r1", session_id="s1"):
    return E2AEnvelope(request_id=rid, session_id=session_id, channel="web",
                       method="chat.send", params={"query": query})


def _engine(tmp_path):
    policy = PermissionPolicy(tools={"echo": "require-approval"}, rules=[],
                             approval_overrides={}, global_default="allow",
                             overrides_file=str(tmp_path / "ovr.json"))
    return PermissionEngine(policy=policy, audit=ToolPermissionLog(str(tmp_path / "a.jsonl")),
                            enabled=True, enabled_channels={"web"})


def test_ask_then_allow_resumes_and_executes(session_store, tmp_path) -> None:
    APPROVAL_REGISTRY.cancel_all()
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    tm = ToolManager(); tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]})],
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm, LongTermMemory(), permission=_engine(tmp_path))
    loop.register_hook(PermissionHook(loop._permission))

    async def run():
        frames = []
        async for f in loop.run_stream(_env("call echo")):
            frames.append(f)
            if f.response_kind == "e2a.ask":
                APPROVAL_REGISTRY.resolve(f.body["approval_id"], "allow")
        return frames

    frames = asyncio.run(run())
    ask = [f for f in frames if f.response_kind == "e2a.ask"][0]
    assert ask.body["tool"] == "echo" and ask.is_final is False
    assert frames[-1].response_kind == "e2a.complete"
    msgs = session_store.get_messages("s1")
    assert msgs[3]["role"] == "tool" and msgs[3]["content"] == "tool-saw:hi"


def test_ask_then_denied_injects_deny_result(session_store, tmp_path) -> None:
    APPROVAL_REGISTRY.cancel_all()
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return "should-not-run"
    tm = ToolManager(); tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "denied-ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm, LongTermMemory(), permission=_engine(tmp_path))
    loop.register_hook(PermissionHook(loop._permission))

    async def run():
        frames = []
        async for f in loop.run_stream(_env("call echo")):
            frames.append(f)
            if f.response_kind == "e2a.ask":
                APPROVAL_REGISTRY.resolve(f.body["approval_id"], "deny")
        return frames

    frames = asyncio.run(run())
    msgs = session_store.get_messages("s1")
    assert msgs[3]["role"] == "tool"
    assert "denied by user" in msgs[3]["content"]
    assert frames[-1].response_kind == "e2a.complete"


def test_allow_always_persists_then_skips_next_ask(session_store, tmp_path) -> None:
    APPROVAL_REGISTRY.cancel_all()
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    tm = ToolManager(); tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"a"}'}}]})],
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c2", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"b"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm, LongTermMemory(), permission=_engine(tmp_path))
    loop.register_hook(PermissionHook(loop._permission))

    async def run():
        frames = []
        async for f in loop.run_stream(_env("twice")):
            frames.append(f)
            if f.response_kind == "e2a.ask":
                APPROVAL_REGISTRY.resolve(f.body["approval_id"], "allow_always")
        return frames

    frames = asyncio.run(run())
    asks = [f for f in frames if f.response_kind == "e2a.ask"]
    assert len(asks) == 1
    assert frames[-1].response_kind == "e2a.complete"
