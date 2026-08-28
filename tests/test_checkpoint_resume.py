"""checkpoint.json + 请求驱动 resume 的测试。

验证:
- resume 时从 checkpoint.json 灌回压缩窗口快照(不重读全量 history);
- 每步 save checkpoint.json;
- 无 checkpoint / 损坏 checkpoint → fail-soft fallback 从 history 冷加载(现状不回归)。
"""
import asyncio
import json

from twinkle.agentserver.agent import ReActAgent as AgentLoop
from twinkle.agentserver.agent import AgentRequest
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def stream(self, messages, tools):
        self.seen_messages.append(messages)
        evs = self._scripts[self.calls]
        self.calls += 1
        for ev in evs:
            yield ev


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(session_id=session_id, request_id=request_id, query=query)


async def _collect(gen):
    return [f async for f in gen]


def _echo_tool():
    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"
    tm = ToolManager()
    tm.register(echo)
    return tm


def test_resume_restores_from_checkpoint_snapshot(session_store) -> None:
    """崩溃残留:checkpoint.json 有压缩窗口快照(含孤儿 tool_call),history 有
    全量(含会被压缩的早期原文)。resume 应灌回 checkpoint 快照,使 LLM 看到
    checkpoint 视图(不是 history 全量)+ _fill_missing 修孤儿 tool_call。"""
    # history 全量(含早期原文 from_history)
    asyncio.run(session_store.append("s1", {"role": "user", "content": "原始早期长文本 from_history"}))
    asyncio.run(session_store.append("s1", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]}))
    # checkpoint 快照:压缩窗口(早期已摘要成 from_checkpoint,孤儿 c1 仍在 tail)
    session_store.save_checkpoint("s1", [
        {"role": "user", "content": "压缩后早期 from_checkpoint"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]},
    ], request_id="prev")
    tm = _echo_tool()
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("resume", session_id="s1"))))
    # LLM 收到 checkpoint 视图(from_checkpoint),不是 history 全量(from_history)
    rendered = json.dumps(llm.seen_messages[0], ensure_ascii=False)
    assert "from_checkpoint" in rendered, "resume 应灌回 checkpoint 快照"
    assert "from_history" not in rendered, "resume 不应重读 history 全量"
    # 孤儿 c1 被 _fill_missing 修(合成 interrupted tool_result)
    msgs = session_store.get_messages("s1")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any("interrupted" in m["content"] for m in tool_msgs), "孤儿 tool_call 应被修复"


def test_save_checkpoint_each_step(session_store) -> None:
    """每步 save checkpoint.json;最后快照含上一步 tool result。"""
    tm = _echo_tool()
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("do echo", session_id="s1"))))
    cp = session_store.load_checkpoint("s1")
    assert cp is not None, "checkpoint.json 应被 save"
    assert cp["request_id"] == "r1"
    # 最后 save 在第二步 before_model_call 后,含第一步的 tool_result
    rendered = json.dumps(cp["messages"], ensure_ascii=False)
    assert "tool-saw:hi" in rendered, "checkpoint 快照应含上一步 tool result"


def test_fallback_no_checkpoint_reads_history(session_store) -> None:
    """无 checkpoint.json → 现状:从 history 冷加载(不回归)。"""
    asyncio.run(session_store.append("s1", {"role": "user", "content": "from_history_only"}))
    tm = _echo_tool()
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("hi", session_id="s1"))))
    rendered = json.dumps(llm.seen_messages[0], ensure_ascii=False)
    assert "from_history_only" in rendered, "无 checkpoint 时应从 history 加载"


def test_corrupt_checkpoint_falls_back(session_store) -> None:
    """checkpoint.json 损坏 → fail-soft 返回 None → fallback 从 history。"""
    asyncio.run(session_store.append("s1", {"role": "user", "content": "from_history"}))
    ckpt_path = session_store._checkpoint_path("s1")
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.write_text("{not valid json", encoding="utf-8")
    tm = _echo_tool()
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)
    asyncio.run(_collect(loop.run(_env("hi", session_id="s1"))))
    rendered = json.dumps(llm.seen_messages[0], ensure_ascii=False)
    assert "from_history" in rendered, "损坏 checkpoint 应 fail-soft fallback 到 history"
