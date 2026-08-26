import asyncio

from twinkle.agentserver.agent import ReActAgent as AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.agent import AgentRequest


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts; self.calls = 0
    async def stream(self, messages, tools):
        evs = self._scripts[self.calls]; self.calls += 1
        for ev in evs:
            yield ev


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(session_id=session_id, request_id=request_id, query=query)


def test_orphan_assistant_tool_calls_sanitized(session_store) -> None:
    # 种入一个 orphan：assistant(tool_calls) 没有 tool result（模拟 approval 中途崩溃）
    asyncio.run(session_store.append("s1", {"role": "system", "content": "sys"}))
    asyncio.run(session_store.append("s1", {"role": "user", "content": "do x"}))
    asyncio.run(session_store.append("s1", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]}))
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    tm = ToolManager(); tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "recovered", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("resume", session_id="s1"))))
    msgs = session_store.get_messages("s1")
    roles = [m["role"] for m in msgs]
    assert "tool" in roles  # orphan 得到了一个合成的 tool result
    assert roles[-1] == "assistant" and msgs[-1]["content"] == "recovered"


def test_mid_batch_orphan_sanitized(session_store) -> None:
    # 批量中途崩溃：c1 已执行 + result 已追加，c2 遇到 ASK + 在挂起时崩溃。
    # 最后一条是 `tool`（c1 的 result），不是 assistant —— 旧的 sanitize 在这里就退出了。
    asyncio.run(session_store.append("s1", {"role": "system", "content": "sys"}))
    asyncio.run(session_store.append("s1", {"role": "user", "content": "do x and y"}))
    asyncio.run(session_store.append("s1", {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text":"x"}'}},
            {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"text":"y"}'}},
        ]}))
    asyncio.run(session_store.append("s1", {"role": "tool", "tool_call_id": "c1", "content": "tool-saw:x"}))
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    tm = ToolManager(); tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "recovered", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("resume", session_id="s1"))))
    msgs = session_store.get_messages("s1")
    # c1 的真实 result 保留；c2 的合成 result 注入（这是 I-1 修复）
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "c1" and tool_msgs[0]["content"] == "tool-saw:x"
    assert tool_msgs[1]["tool_call_id"] == "c2"
    assert "interrupted" in tool_msgs[1]["content"]
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "recovered"


async def _collect(gen):
    return [f async for f in gen]
