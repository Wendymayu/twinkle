import asyncio
import json

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.registry import ToolRegistry
from twinkle.e2a.models import E2AEnvelope


class _ScriptedLLM:
    """Returns one canned event-list per call, in order."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _env(query, rid="r1", session_id="s1"):
    return E2AEnvelope(
        request_id=rid,
        session_id=session_id,
        method="chat.send",
        params={"query": query},
    )


def _reg_with_echo_tool():
    reg = ToolRegistry()

    async def echo(text: str) -> str:
        return f"tool-saw:{text}"

    reg.register(
        "echo",
        "echo",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo,
    )
    return reg


def test_plain_answer_streams_chunks_and_complete() -> None:
    store = SessionStore()
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool(), LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"
    assert final.body["result"]["content"] == "hello"


def test_tool_call_round_trip_then_answer() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    llm = _ScriptedLLM([
        # turn 1: model calls echo
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2: model produces final answer
        [TextDelta("result was "), TextDelta("good"),
         Finish("stop", {"role": "assistant", "content": "result was good", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("call echo"))]
        return frames

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "good" in final.body["result"]["content"]

    # session store now holds user, assistant(tool_calls), tool, assistant(answer)
    msgs = store.get_messages("s1")
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"]
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "c1"
    assert msgs[2]["content"] == "tool-saw:hi"
    assert msgs[3]["role"] == "assistant"


def test_cross_turn_remembers_context() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    seen_messages = []

    class _CapturingLLM:
        def __init__(self, scripts):
            self._scripts = scripts
            self.calls = 0

        async def stream(self, messages, tools):
            seen_messages.append([dict(m) for m in messages])
            events = self._scripts[self.calls]
            self.calls += 1
            for ev in events:
                yield ev

    llm = _CapturingLLM([
        [Finish("stop", {"role": "assistant", "content": "ack1", "tool_calls": None})],
        [Finish("stop", {"role": "assistant", "content": "ack2", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        async for _ in loop.run_stream(_env("turn1", rid="r1", session_id="s1")):
            pass
        async for _ in loop.run_stream(_env("turn2", rid="r2", session_id="s1")):
            pass

    asyncio.run(run())
    # turn 2's messages include turn 1's user + assistant
    assert len(seen_messages[0]) == 1   # [user]
    assert len(seen_messages[1]) == 3   # [user, assistant, user]
    assert seen_messages[1][0]["content"] == "turn1"
    assert seen_messages[1][1]["content"] == "ack1"
    assert seen_messages[1][2]["content"] == "turn2"


def test_max_steps_emits_error() -> None:
    store = SessionStore()
    reg = _reg_with_echo_tool()
    # every turn asks for a tool call -> never converges
    tool_finish = Finish("tool_calls", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "x"}'}}]})
    llm = _ScriptedLLM([ [tool_finish] for _ in range(20) ])
    loop = AgentLoop(llm, store, reg, LongTermMemory())

    async def run():
        frames = [f async for f in loop.run_stream(_env("loop"))]
        return frames

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.error"
    assert frames[-1].status == "failed"
