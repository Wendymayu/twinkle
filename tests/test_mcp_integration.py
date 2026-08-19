import pytest

from twinkle.agentserver.mcp import get_mcp_manager, _set_mcp_manager
from twinkle.agentserver.mcp.manager import McpManager
from twinkle.config.schema import McpConfig


@pytest.fixture(autouse=True)
def _reset_mcp_singleton():
    yield
    _set_mcp_manager(None)


class _FakeTool:
    def __init__(self, name):
        from twinkle.agentserver.mcp.tool import McpToolCard
        self.card = McpToolCard(name=name, server_name=name.split(".")[0],
                                description="d", parameters={"type": "object"})
    async def invoke(self, args):
        return f"mcp-result:{self.card.name}"


def _mgr_with_tools(*names):
    mgr = McpManager(McpConfig(enabled=True))
    for n in names:
        mgr._tools[n] = _FakeTool(n)
    return mgr


def test_create_agent_injects_mcp_tools(session_store, tmp_path) -> None:
    _set_mcp_manager(_mgr_with_tools("my.search", "my.read"))
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[])
    names = {t.card.name for t in agent._tool_manager.list()}
    assert "my.search" in names and "my.read" in names


def test_create_agent_no_mcp_still_works(session_store, tmp_path) -> None:
    _set_mcp_manager(None)  # mcp disabled/no-started → register_into no-op
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[])
    names = {t.card.name for t in agent._tool_manager.list()}
    assert "web_fetch" in names  # builtin 仍在
    assert not any(n.startswith("my.") for n in names)


def test_agent_loop_calls_mcp_tool_and_inlines_result(session_store, tmp_path) -> None:
    """ScriptedLLM 调 {server}.{tool} → ToolManager.execute → 结果回灌 session。"""
    import asyncio
    from twinkle.agentserver.agent import AgentRequest
    from twinkle.agentserver.llm_client import Finish

    _set_mcp_manager(_mgr_with_tools("my.search"))

    class _ScriptedLLM:
        def __init__(self, scripts): self._s = scripts; self.calls = 0
        async def stream(self, messages, tools):
            evs = self._s[self.calls]; self.calls += 1
            for ev in evs: yield ev

    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "my.search",
                                         "arguments": '{"q": "x"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    from twinkle.agentserver.server import create_agent
    agent = create_agent(session_store, hooks=[], llm=llm)

    req = AgentRequest(session_id="s1", request_id="r1", query="search x")
    frames = []
    async def run():
        async for f in agent.run(req):
            frames.append(f)
    asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"
    msgs = session_store.get_messages("s1")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "mcp-result:my.search"
    assert tool_msgs[0]["tool_call_id"] == "c1"
