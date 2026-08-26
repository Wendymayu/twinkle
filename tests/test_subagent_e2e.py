"""subagent 特性的端到端集成测试(Task 13 —— 收官)。

证明完整链路端到端可用:
  - Parent 通过 spawn_subagent 委派。
  - Child 在隔离 session 中跑自己的 ReAct(fresh context,看不到 parent
    history;自带 system prompt + 工具集,减去 spawn_subagent / memory-writes)。
  - Child 的最终答案作为 {role:"tool"} 重新注入 PARENT session,
    并追加 [SYSTEM] 停止提示。
  - Parent 概括 child 结果(最终 e2a.complete 含其内容)。
  - 同一轮的两次 spawn_subagent 调用在 for-tc loop 中顺序执行,
    互不污染对方的 ContextVars / session。

CRITICAL —— full_text 陷阱:AgentLoop._inner_run_stream 发出
e2a.complete.body.result.content = full_text,其中 full_text 是
累积的 TextDelta content(agent_loop.py:~273),而非
ev.assistant_message.content。故每个 child 脚本和 parent 的最终
轮次都包含 TextDelta(...) 调用以让 full_text 累积 content。
切勿剥离 TextDelta 调用。
"""
import asyncio

from twinkle.agentserver.agent import ReActAgent as AgentLoop
from twinkle.agentserver.hooks.builtin.subagent_context_hook import SubagentContextHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.tools import tool_manager
from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
from twinkle.agentserver.tools.builtin.subagent import SubagentExecutor
from twinkle.config.schema import SubagentConfig
from twinkle.agentserver.agent import AgentRequest


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _env(query, session_id="parent", request_id="r1"):
    return AgentRequest(session_id=session_id, request_id=request_id, query=query)


def test_parent_delegates_then_summarizes(session_store):
    """Parent 调用 spawn_subagent;child 跑自己的 ReAct 并返回最终
    答案;该答案作为 {role:"tool"} 重新注入;parent 概括。"""
    # PARENT LLM: turn 1 -> 调用 spawn_subagent;turn 2 -> 概括 child 结果
    parent_llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "spawn_subagent",
                                           "arguments": '{"objective": "find the answer", "prompt": ""}'}}]})],
        [TextDelta("the child said: "), TextDelta("42"),
         Finish("stop", {"role": "assistant", "content": "the child said: 42", "tool_calls": None})],
    ])
    # CHILD LLM: 一次性直答(TextDelta 以使 full_text 累积 "42")
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
        return [f async for f in loop.run(_env("what is the answer?"))]

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "42" in final.body["result"]["content"]

    # child 结果已作为 {role:"tool"} 重新注入 PARENT session
    parent_msgs = session_store.get_messages("parent")
    roles = [m["role"] for m in parent_msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = parent_msgs[2]
    assert tool_msg["role"] == "tool"
    assert "42" in tool_msg["content"]
    assert "[SYSTEM]" in tool_msg["content"]              # 追加停止提示

    # 已创建一个 child session,默认对 list_sessions 隐藏
    default_ids = {r["session_id"] for r in session_store.list_sessions()}
    assert "parent" in default_ids
    assert not any("__sub_" in s for s in default_ids)


def test_concurrent_spawns_do_not_cross_contaminate(session_store):
    """两次 spawn_subagent 调用(在 for-tc loop 中顺序执行)不得污染
    对方的 ContextVars / session。"""
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
    # CHILD LLM: 调用 1 -> "A-result",调用 2 -> "B-result"(TextDelta 以使 full_text 累积)
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
        return [f async for f in loop.run(_env("do both", session_id="p2"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"
    parent_msgs = session_store.get_messages("p2")
    tool_msgs = [m for m in parent_msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    contents = "".join(m["content"] for m in tool_msgs)
    assert "A-result" in contents and "B-result" in contents
    # 两个不同的 child session
    all_ids = [r["session_id"] for r in session_store.list_sessions(include_subagents=True)]
    child_ids = [s for s in all_ids if s.startswith("p2__sub_")]
    assert len(child_ids) == 2
