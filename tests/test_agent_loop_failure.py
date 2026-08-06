"""AgentLoop failure handling: tool-exception catch + RetryHook retry.

Covers the three behaviours added to fix the failure-handling gaps:
- A tool raising a non-transient exception becomes a "[tool error] ..." tool_result
  and the loop continues (does not crash) — the catch lives in agent_loop, not
  ToolManager.execute.
- A tool raising a transient exception is retried once (by RetryHook via @hook),
  then if still failing becomes a "[tool error] ..." tool_result.
- A model call raising a transient exception is retried once, with a backoff
  sleep before the retry.
"""
from __future__ import annotations

import asyncio

import httpx

from twinkle.agentserver.agent import ReActAgent as AgentLoop
from twinkle.agentserver.hooks.builtin.retry_hook import RetryHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.agentserver.agent import AgentRequest


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(
        session_id=session_id, request_id=request_id, query=query,
    )


class _ScriptedLLM:
    """Returns one canned event-list per stream() call, in order."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _tool_call_finish(name, args_json, tc_id="c1"):
    return Finish("tool_calls", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": tc_id, "type": "function",
                        "function": {"name": name, "arguments": args_json}}],
    })


def _final_answer(text="ok"):
    return [TextDelta(text), Finish("stop", {
        "role": "assistant", "content": text, "tool_calls": None,
    })]


def _reg_with(tool_fn):
    m = ToolManager()
    m.register(tool_fn)
    return m


def test_tool_non_transient_becomes_tool_error_and_loop_continues(session_store):
    """Non-transient tool exception (ValueError) is NOT retried; it becomes a
    "[tool error] ..." tool_result and the loop continues to a final answer."""
    store = session_store
    calls = {"n": 0}

    @tool
    async def boom(x: str) -> str:
        """boom"""
        calls["n"] += 1
        raise ValueError("boom")

    llm = _ScriptedLLM([
        [_tool_call_finish("boom", '{"x": "1"}')],
        _final_answer("ok"),
    ])
    loop = AgentLoop(llm, store, _reg_with(boom))
    loop.register_hook(RetryHook(delay=0))

    async def run():
        return [f async for f in loop.run(_env("call boom"))]

    frames = asyncio.run(run())

    assert calls["n"] == 1                       # no retry for non-transient
    assert frames[-1].response_kind == "e2a.complete"
    tool_msgs = [m for m in store.get_messages("s1") if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "[tool error] ValueError: boom"


def test_tool_transient_retried_once_then_becomes_tool_error(session_store):
    """Transient tool exception (httpx.ConnectError) is retried once (called
    twice total); when it keeps failing it becomes a "[tool error] ..." tool_result
    and the loop continues."""
    store = session_store
    calls = {"n": 0}

    @tool
    async def flaky(url: str) -> str:
        """flaky"""
        calls["n"] += 1
        raise httpx.ConnectError("net down")

    llm = _ScriptedLLM([
        [_tool_call_finish("flaky", '{"url": "http://x"}')],
        _final_answer("ok"),
    ])
    loop = AgentLoop(llm, store, _reg_with(flaky))
    loop.register_hook(RetryHook(delay=0))

    async def run():
        return [f async for f in loop.run(_env("call flaky"))]

    frames = asyncio.run(run())

    assert calls["n"] == 2                       # 1 retry for transient
    assert frames[-1].response_kind == "e2a.complete"
    tool_msgs = [m for m in store.get_messages("s1") if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].startswith("[tool error]")
    assert "ConnectError" in tool_msgs[0]["content"]


class _FlakyLLM:
    """Raises asyncio.TimeoutError on the first stream() call, then succeeds."""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise asyncio.TimeoutError()
        for ev in self._scripts[0]:
            yield ev


def test_model_transient_retried_with_backoff_sleep(session_store, monkeypatch):
    """A transient model exception is retried once, and the retry loop sleeps
    the retry delay (backoff) before retrying."""
    store = session_store
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    llm = _FlakyLLM([_final_answer("ok")])
    loop = AgentLoop(llm, store, _reg_with(_echo_tool))
    loop.register_hook(RetryHook(max_retries=1, delay=0.5))

    async def run():
        return [f async for f in loop.run(_env("hi"))]

    frames = asyncio.run(run())

    assert llm.calls == 2                         # 1 retry
    assert frames[-1].response_kind == "e2a.complete"
    assert 0.5 in sleeps                          # backoff slept before retry


@tool
async def _echo_tool(text: str) -> str:
    """echo"""
    return f"echo:{text}"
