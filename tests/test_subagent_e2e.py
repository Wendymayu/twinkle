"""End-to-end integration test for the subagent feature (Task 13 — capstone).

Proves the full wiring works end-to-end:
  - Parent delegates via spawn_subagent.
  - Child runs its own ReAct in an isolated session (fresh context, can't see
    parent history; its own system prompt + tool set minus spawn_subagent /
    memory-writes).
  - The child's final answer is re-injected into the PARENT session as
    {role:"tool"} with the [SYSTEM] stop hint appended.
  - The parent summarizes the child result (final e2a.complete contains it).
  - Two spawn_subagent calls in one turn run sequentially in the for-tc loop
    and do not cross-contaminate each other's ContextVars / sessions.

CRITICAL — the full_text gotcha: AgentLoop._inner_run_stream emits
e2a.complete.body.result.content = full_text, where full_text is the
ACCUMULATED TextDelta content (agent_loop.py:~273), NOT
ev.assistant_message.content. So every child script AND the parent's final
turn include TextDelta(...) calls so full_text accumulates the content. Do
NOT strip the TextDelta calls.
"""
import asyncio

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.hooks.builtin.subagent_context_hook import SubagentContextHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor
from twinkle.config.schema import SubagentConfig
from twinkle.e2a.models import E2AEnvelope


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _env(query, sid="parent", rid="r1"):
    return E2AEnvelope(request_id=rid, session_id=sid, method="chat.send",
                       params={"query": query})


def test_parent_delegates_then_summarizes(session_store):
    """Parent calls spawn_subagent; child runs its own ReAct and returns a final
    answer; that answer is re-injected as {role:"tool"}; parent summarizes."""
    # PARENT LLM: turn 1 -> call spawn_subagent; turn 2 -> summarize the child result
    parent_llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "spawn_subagent",
                                           "arguments": '{"objective": "find the answer", "prompt": ""}'}}]})],
        [TextDelta("the child said: "), TextDelta("42"),
         Finish("stop", {"role": "assistant", "content": "the child said: 42", "tool_calls": None})],
    ])
    # CHILD LLM: one straight answer (TextDelta so full_text accumulates "42")
    child_llm = _ScriptedLLM([
        [TextDelta("42"), Finish("stop", {"role": "assistant", "content": "42", "tool_calls": None})],
    ])

    parent_tm = tool_manager()
    executor = SubagentExecutor(
        llm=child_llm, store=session_store, parent_tools=parent_tm,
        config=SubagentConfig(), child_hooks=[],
    )
    loop = AgentLoop(parent_llm, session_store, parent_tm)
    loop.register_hook(SubagentContextHook(executor))

    async def run():
        return [f async for f in loop.run_stream(_env("what is the answer?"))]

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "42" in final.body["result"]["content"]

    # The child result was re-injected into the PARENT session as {role:"tool"}
    parent_msgs = session_store.get_messages("parent")
    roles = [m["role"] for m in parent_msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    tool_msg = parent_msgs[3]
    assert tool_msg["role"] == "tool"
    assert "42" in tool_msg["content"]
    assert "[SYSTEM]" in tool_msg["content"]              # stop hint appended

    # A child session was created and is hidden from list_sessions by default
    default_ids = {r["session_id"] for r in session_store.list_sessions()}
    assert "parent" in default_ids
    assert not any("__sub_" in s for s in default_ids)


def test_concurrent_spawns_do_not_cross_contaminate(session_store):
    """Two spawn_subagent calls (sequential in the for-tc loop) must not pollute
    each other's ContextVars / sessions."""
    parent_llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [
                  {"id": "c1", "type": "function",
                   "function": {"name": "spawn_subagent",
                                "arguments": '{"objective": "task A", "prompt": ""}'}},
                  {"id": "c2", "type": "function",
                   "function": {"name": "spawn_subagent",
                                "arguments": '{"objective": "task B", "prompt": ""}'}},
              ]})],
        [TextDelta("both done"), Finish("stop", {"role": "assistant", "content": "both done", "tool_calls": None})],
    ])
    # CHILD LLM: call 1 -> "A-result", call 2 -> "B-result" (TextDelta so full_text accumulates)
    child_llm = _ScriptedLLM([
        [TextDelta("A-result"), Finish("stop", {"role": "assistant", "content": "A-result", "tool_calls": None})],
        [TextDelta("B-result"), Finish("stop", {"role": "assistant", "content": "B-result", "tool_calls": None})],
    ])
    parent_tm = tool_manager()
    executor = SubagentExecutor(llm=child_llm, store=session_store,
                                parent_tools=parent_tm, config=SubagentConfig(),
                                child_hooks=[])
    loop = AgentLoop(parent_llm, session_store, parent_tm)
    loop.register_hook(SubagentContextHook(executor))

    async def run():
        return [f async for f in loop.run_stream(_env("do both", sid="p2"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"
    parent_msgs = session_store.get_messages("p2")
    tool_msgs = [m for m in parent_msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    contents = "".join(m["content"] for m in tool_msgs)
    assert "A-result" in contents and "B-result" in contents
    # two distinct child sessions
    all_ids = [r["session_id"] for r in session_store.list_sessions(include_subagents=True)]
    child_ids = [s for s in all_ids if s.startswith("p2__sub_")]
    assert len(child_ids) == 2
