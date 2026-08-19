# tests/test_mcp_permissions.py
import asyncio

import pytest

from twinkle.agentserver.agent import AgentRequest
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.mcp import _set_mcp_manager
from twinkle.agentserver.mcp.manager import McpManager
from twinkle.agentserver.mcp.tool import McpToolCard
from twinkle.agentserver.permissions.approval_registry import APPROVAL_REGISTRY
from twinkle.agentserver.permissions.audit import ToolPermissionLog
from twinkle.agentserver.permissions.engine import PermissionEngine
from twinkle.agentserver.permissions.policy import PermissionPolicy
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.config.schema import McpConfig


class _McpFakeTool:
    def __init__(self, name):
        self.card = McpToolCard(name=name, server_name=name.split(".")[0],
                                description="d", parameters={"type": "object"})
    async def invoke(self, args):
        return f"ran:{self.card.name}"


def _engine_with(tmp_path, tools_tier):
    policy = PermissionPolicy(tools=tools_tier, rules=[], approval_overrides={},
                              global_default="allow",
                              overrides_file=str(tmp_path / "ovr.json"))
    return PermissionEngine(policy=policy, audit=ToolPermissionLog(str(tmp_path / "a.jsonl")),
                            enabled=True, enabled_channels={"web"})


def _scripted(ask_tool):
    return _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": ask_tool, "arguments": '{}'}}]})],
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])


class _ScriptedLLM:
    def __init__(self, scripts): self._s = scripts; self.calls = 0
    async def stream(self, messages, tools):
        evs = self._s[self.calls]; self.calls += 1
        for ev in evs: yield ev


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(session_id=session_id, request_id=request_id, query=query)


@pytest.fixture(autouse=True)
def _reset_mcp_and_approvals():
    APPROVAL_REGISTRY.cancel_all()
    yield
    _set_mcp_manager(None)
    APPROVAL_REGISTRY.cancel_all()


def test_mcp_tool_require_approval_asks_then_allows(session_store, tmp_path) -> None:
    mgr = McpManager(McpConfig(enabled=True))
    mgr._tools["my.search"] = _McpFakeTool("my.search")
    _set_mcp_manager(mgr)
    llm = _scripted("my.search")
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store,
                         hooks=[PermissionHook(_engine_with(tmp_path, {"my.search": "require-approval"}))],
                         llm=llm)

    async def run():
        frames = []
        async for f in agent.run(_env("search")):
            frames.append(f)
            if f.response_kind == "e2a.ask":
                APPROVAL_REGISTRY.resolve(f.body["approval_id"], "allow")
        return frames
    frames = asyncio.run(run())
    ask = [f for f in frames if f.response_kind == "e2a.ask"][0]
    assert ask.body["tool"] == "my.search"
    assert frames[-1].response_kind == "e2a.complete"
    msgs = session_store.get_messages("s1")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "ran:my.search"


def test_mcp_tool_unconfigured_falls_to_global_allow(session_store, tmp_path) -> None:
    """未在 permissions.tools 配的 MCP 工具走 global_default(allow),不 ASK。"""
    mgr = McpManager(McpConfig(enabled=True))
    mgr._tools["my.search"] = _McpFakeTool("my.search")
    _set_mcp_manager(mgr)
    llm = _scripted("my.search")
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[PermissionHook(_engine_with(tmp_path, {}))], llm=llm)
    frames = []

    async def run():
        async for f in agent.run(_env("search")):
            frames.append(f)
        return frames
    asyncio.run(run())
    assert not any(f.response_kind == "e2a.ask" for f in frames)
    assert frames[-1].response_kind == "e2a.complete"
    tool_msgs = [m for m in session_store.get_messages("s1") if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "ran:my.search"
