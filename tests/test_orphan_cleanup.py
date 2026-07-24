import asyncio

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
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
    return E2AEnvelope(request_id=rid, session_id=session_id, method="chat.send",
                       params={"query": query})


def test_orphan_assistant_tool_calls_sanitized(session_store) -> None:
    # seed an orphan: assistant(tool_calls) with NO tool result (simulating a crash mid-approval)
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
    loop = AgentLoop(llm, session_store, tm, LongTermMemory())
    asyncio.run(_collect(loop.run_stream(_env("resume", session_id="s1"))))
    msgs = session_store.get_messages("s1")
    roles = [m["role"] for m in msgs]
    assert "tool" in roles  # the orphan got a synthetic tool result
    assert roles[-1] == "assistant" and msgs[-1]["content"] == "recovered"


async def _collect(gen):
    return [f async for f in gen]
