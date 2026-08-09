import asyncio

from twinkle.agentserver.agent import AgentRequest, ReActAgent
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.team.message_box import MessageBox
from twinkle.agentserver.tools.manager import ToolManager


class _RecordingLLM:
    """Scripted LLM that records the messages it received per call."""

    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.received_messages: list = []

    async def stream(self, messages, tools):
        self.received_messages = list(messages)
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _run_agent(store, llm, inbox, query="do task", sid="s1"):
    agent = ReActAgent(llm, store, ToolManager(), inbox=inbox)

    async def _go():
        await store.create_session(sid)
        async for _ in agent.run(AgentRequest(session_id=sid, request_id="r1", query=query)):
            pass

    asyncio.run(_go())


def test_inbox_message_reaches_llm_but_not_session_store(session_store):
    box = MessageBox()
    box.put("steer: add risk section")
    llm = _RecordingLLM([
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    _run_agent(session_store, llm, box)

    # LLM 收到的 messages 含 steer
    assert any("steer: add risk section" in (m.get("content") or "")
               for m in llm.received_messages)
    # session store 不含 steer(不进历史)
    history = session_store.get_history("s1")
    assert not any("steer: add risk section" in (m.get("content") or "")
                   for m in history)


def test_no_inbox_does_not_break_run(session_store):
    llm = _RecordingLLM([
        [TextDelta("done"), Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    _run_agent(session_store, llm, inbox=None)
    assert llm.calls == 1
