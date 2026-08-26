import asyncio
import json

from twinkle.agentserver.agent import AgentRequest, ReActAgent as AgentLoop
from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.agentserver.tools.decorator import tool


class _ScriptedLLM:
    """每次调用按顺序返回一组预设事件列表。"""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _env(query, request_id="r1", session_id="s1"):
    return AgentRequest(
        session_id=session_id,
        request_id=request_id,
        query=query,
    )


def _reg_with_echo_tool():
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    m = ToolManager()
    m.register(echo)
    return m


def test_plain_answer_streams_chunks_and_complete(session_store) -> None:
    store = session_store
    llm = _ScriptedLLM([
        [TextDelta("hel"), TextDelta("lo"),
         Finish("stop", {"role": "assistant", "content": "hello", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool())

    async def run():
        frames = [f async for f in loop.run(_env("hi"))]
        return frames

    frames = asyncio.run(run())
    chunks = [f for f in frames if not f.is_final]
    final = frames[-1]
    assert "".join(c.body["result"]["content"] for c in chunks) == "hello"
    assert final.is_final
    assert final.response_kind == "e2a.complete"
    assert final.body["result"]["content"] == "hello"


def test_tool_call_round_trip_then_answer(session_store) -> None:
    store = session_store
    reg = _reg_with_echo_tool()
    llm = _ScriptedLLM([
        # turn 1：model 调用 echo
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]})],
        # turn 2：model 给出最终回答
        [TextDelta("result was "), TextDelta("good"),
         Finish("stop", {"role": "assistant", "content": "result was good", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg)

    async def run():
        frames = [f async for f in loop.run(_env("call echo"))]
        return frames

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "good" in final.body["result"]["content"]

    # session store 现含：user、assistant(tool_calls)、tool、assistant(answer)
    # (system prompt 每步注入 LLM messages，不持久化)
    msgs = store.get_messages("s1")
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"]
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "c1"
    assert msgs[2]["content"] == "tool-saw:hi"
    assert msgs[3]["role"] == "assistant"


def test_cross_turn_remembers_context(session_store) -> None:
    store = session_store
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
    loop = AgentLoop(llm, store, reg)

    async def run():
        async for _ in loop.run(_env("turn1", request_id="r1", session_id="s1")):
            pass
        async for _ in loop.run(_env("turn2", request_id="r2", session_id="s1")):
            pass

    asyncio.run(run())
    # turn 2 的 messages 含 turn 1 的 user + assistant，加上 turn 1 的 system msg
    assert len(seen_messages[0]) == 2   # [system, user]
    assert len(seen_messages[1]) == 4   # [system, user, assistant, user]
    assert seen_messages[0][0]["role"] == "system"
    assert seen_messages[1][1]["content"] == "turn1"
    assert seen_messages[1][2]["content"] == "ack1"
    assert seen_messages[1][3]["content"] == "turn2"


def test_unbounded_loop_stops_via_critical_not_step_cap(session_store) -> None:
    """无界主 agent（无 max_steps）+ CRITICAL 重复工具循环
    经循环检测 force_finish 终止——而非经 step cap。证明新刹车
    （CRITICAL 硬停）替代了已移除的 1000 步上限。"""
    from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import (
        RepeatToolCallDetectorHook)
    store = session_store
    reg = _reg_with_echo_tool()
    tool_finish = Finish("tool_calls", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "x"}'}}]})
    llm = _ScriptedLLM([[tool_finish] for _ in range(20)])
    agent = AgentLoop(llm, store, reg, hooks=(RepeatToolCallDetectorHook(
        repeat_warn=10, pingpong_warn=10, loop_block=2, global_stop=3),))

    async def run():
        return [f async for f in agent.run(_env("loop"))]

    frames = asyncio.run(run())
    final = frames[-1]
    # 由循环检测终止（force_finish -> e2a.complete 含 loop msg），
    # 而非由 step cap（任何地方都没有 "exceeded max_steps"）。
    assert final.response_kind == "e2a.complete"
    assert "loop" in final.body["result"]["content"].lower()
    assert not any("exceeded max_steps" in str(f.body) for f in frames)


def test_todo_create_round_trip_through_loop(session_store, isolated_todo_store) -> None:
    """model 调 todo_create 后作答——验证 ContextVar 被设为
    envelope 的 session_id（见下方 store 断言；若不 PLAN_TODO_SESSION_ID.set，
    工具会回退到 "default"，"s-todo" store key 将留空）。system prompt 每步注入
    LLM messages，不持久化进 store。"""
    from twinkle.agentserver.tools import tool_manager

    store = session_store
    llm = _ScriptedLLM([
        # turn 1：model 调用 todo_create
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "tc1", "type": "function",
                              "function": {"name": "todo_create",
                                           "arguments": '{"subjects": ["step one", "step two"]}'}}]})],
        # turn 2：model 作答
        [TextDelta("planned "), TextDelta("it"),
         Finish("stop", {"role": "assistant", "content": "planned it", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, tool_manager())

    async def run():
        return [f async for f in loop.run(_env("plan something", session_id="s-todo"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"
    # 工具结果被重新注入 store
    msgs = store.get_messages("s-todo")
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"]
    assert msgs[2]["role"] == "tool"
    assert "Created 2 todo tasks." in msgs[2]["content"]
    assert "step one" in msgs[2]["content"]
    assert msgs[3]["role"] == "assistant" and msgs[3]["content"] == "planned it"

    # ContextVar 实际被设为 envelope 的 session_id，而非
    # "default" 回退——否则下面两个 store key 都会为空，
    # 只有 "default" 除外。这使 run_stream 的 PLAN_TODO_SESSION_ID.set(...)
    # 成为核心载荷而非可静默跳过。
    # ContextVar 被设为 envelope 的 session_id；loop 的 todo_create
    # 写入共享单例（= isolated_todo_store）。
    assert len(asyncio.run(isolated_todo_store.list("s-todo"))) == 2
    assert asyncio.run(isolated_todo_store.list("default")) == []


def test_todo_update_frame_emitted_on_create(session_store, isolated_todo_store) -> None:
    """run_stream 在 todo_create 执行后产出一个 e2a.todo_update frame，
    携带结构化快照（不只是 markdown 工具字符串）。"""
    from twinkle.agentserver.tools import tool_manager

    store = session_store
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "tc1", "type": "function",
                              "function": {"name": "todo_create",
                                           "arguments": '{"subjects": ["one", "two"]}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, tool_manager())

    async def run():
        return [f async for f in loop.run(_env("plan", session_id="s-upd"))]

    frames = asyncio.run(run())
    todo_frames = [f for f in frames if f.response_kind == "e2a.todo_update"]
    assert len(todo_frames) == 1
    body = todo_frames[0].body
    assert [t["subject"] for t in body["tasks"]] == ["one", "two"]
    assert body["remaining"] == 2
    assert body["total"] == 2
    assert body["tasks"][0]["subject"] == "one"
    # todo_update frame 非 final，且在 final complete 之前
    assert not todo_frames[0].is_final
    assert frames[-1].response_kind == "e2a.complete"


def test_react_agent_has_no_step_cap_parameter() -> None:
    """ReActAgent 无 max_steps 参数——step cap 对
    所有 agent（主 + subagent + team member）都已移除。无界循环经
    CRITICAL 循环检测 force_finish（主/team member）或
    subagent 的 hard_timeout 终止，绝非 step 计数。"""
    import inspect
    params = inspect.signature(AgentLoop.__init__).parameters
    assert "max_steps" not in params


# --- 并行工具调用测试 --- #


def _reg_with_echo_and_slow():
    """注册 echo + slow_echo 工具供并行测试。"""
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    @tool
    async def slow_echo(text: str) -> str:
        """slow_echo — 模拟带延迟的工具"""
        await asyncio.sleep(0.05)
        return f"slow-saw:{text}"

    m = ToolManager()
    m.register(echo)
    m.register(slow_echo)
    return m


def test_parallel_tool_calls_two_echoes(session_store) -> None:
    """同一批两次 echo 工具调用并发执行，两个结果都出现。"""
    store = session_store
    reg = _reg_with_echo_and_slow()
    llm = _ScriptedLLM([
        # turn 1：model 调用 echo 两次
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [
                  {"id": "c1", "type": "function",
                   "function": {"name": "echo", "arguments": '{"text": "alpha"}'}},
                  {"id": "c2", "type": "function",
                   "function": {"name": "echo", "arguments": '{"text": "beta"}'}},
              ]})],
        # turn 2：model 总结
        [TextDelta("both "), TextDelta("done"),
         Finish("stop", {"role": "assistant", "content": "both done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg)

    async def run():
        return [f async for f in loop.run(_env("two echoes", session_id="s-par"))]

    frames = asyncio.run(run())
    final = frames[-1]
    assert final.response_kind == "e2a.complete"
    assert "both done" in final.body["result"]["content"]

    # 两个工具结果按序追加进 session
    msgs = store.get_messages("s-par")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert tool_msgs[0]["content"] == "tool-saw:alpha"
    assert tool_msgs[1]["tool_call_id"] == "c2"
    assert tool_msgs[1]["content"] == "tool-saw:beta"


def test_parallel_tool_calls_one_error_one_ok(session_store) -> None:
    """并行批中，一个工具出错不影响另一个。"""
    from twinkle.agentserver.tools.manager import ToolManager

    store = session_store

    @tool
    async def good_tool() -> str:
        """good_tool"""
        return "good-result"

    @tool
    async def bad_tool() -> str:
        """bad_tool"""
        raise ValueError("something went wrong")

    reg = ToolManager()
    reg.register(good_tool)
    reg.register(bad_tool)

    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [
                  {"id": "c1", "type": "function",
                   "function": {"name": "good_tool", "arguments": '{}'}},
                  {"id": "c2", "type": "function",
                   "function": {"name": "bad_tool", "arguments": '{}'}},
              ]})],
        [TextDelta("done"), Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg)

    async def run():
        return [f async for f in loop.run(_env("mixed", session_id="s-mix"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"

    msgs = store.get_messages("s-mix")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    # good_tool 成功
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert tool_msgs[0]["content"] == "good-result"
    # bad_tool 错误被捕获为字符串
    assert tool_msgs[1]["tool_call_id"] == "c2"
    assert "ValueError" in tool_msgs[1]["content"]


def test_parallel_tool_calls_disabled(session_store) -> None:
    """批中单次工具调用走顺序路径（无 gather 开销）。"""
    store = session_store
    reg = _reg_with_echo_and_slow()
    llm = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [
                  {"id": "c1", "type": "function",
                   "function": {"name": "echo", "arguments": '{"text": "solo"}'}},
              ]})],
        [TextDelta("done"), Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, reg)

    async def run():
        return [f async for f in loop.run(_env("single", session_id="s-solo"))]

    frames = asyncio.run(run())
    assert frames[-1].response_kind == "e2a.complete"

    msgs = store.get_messages("s-solo")
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "tool-saw:solo"


# --- Phase 12：中断恢复测试 --- #


class _FailingLLM:
    """首次 stream() 调用即抛 RuntimeError。"""
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        raise RuntimeError("model API unreachable")
        yield  # make this an async generator


def test_interrupt_snapshot_on_model_exception(session_store) -> None:
    """model 抛异常时，run_stream 的 finally 块向 session 写一条
    中断快照 assistant message，使 LLM 在下次请求时能理解发生了什么。"""
    store = session_store
    llm = _FailingLLM()
    loop = AgentLoop(llm, store, _reg_with_echo_tool())

    async def run():
        try:
            [f async for f in loop.run(_env("hi", session_id="s-int"))]
        except RuntimeError:
            pass  # expected

    asyncio.run(run())
    msgs = store.get_messages("s-int")
    # 应含：system、user，然后中断快照
    assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert "[SYSTEM] 任务中断" in assistant_msgs[0]["content"]
    assert "RuntimeError" in assistant_msgs[0]["content"]


def test_no_interrupt_snapshot_on_normal_completion(session_store) -> None:
    """请求正常完成时，不写中断快照。"""
    store = session_store
    llm = _ScriptedLLM([
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, store, _reg_with_echo_tool())

    async def run():
        return [f async for f in loop.run(_env("hi", session_id="s-ok"))]

    asyncio.run(run())
    msgs = store.get_messages("s-ok")
    # 不应存在 [SYSTEM] 任务中断 messages
    interrupt_msgs = [m for m in msgs if m.get("role") == "assistant"
                      and "[SYSTEM] 任务中断" in m.get("content", "")]
    assert len(interrupt_msgs) == 0


def test_sanitize_orphan_tool_calls_includes_tool_name_and_args(session_store) -> None:
    """_sanitize_orphan_tool_calls 注入富化上下文：工具名 + 参数。"""
    # 播种孤儿：assistant 带 tool_calls 但无 tool result
    asyncio.run(session_store.append("s-orphan", {"role": "system", "content": "sys"}))
    asyncio.run(session_store.append("s-orphan", {"role": "user", "content": "do x"}))
    asyncio.run(session_store.append("s-orphan", {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]}))

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    from twinkle.agentserver.tools.manager import ToolManager
    tm = ToolManager()
    tm.register(echo)
    llm = _ScriptedLLM([
        [Finish("stop", {"role": "assistant", "content": "recovered", "tool_calls": None})],
    ])
    loop = AgentLoop(llm, session_store, tm)

    async def run():
        return [f async for f in loop.run(_env("resume", session_id="s-orphan"))]

    asyncio.run(run())
    msgs = session_store.get_messages("s-orphan")
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    # 孤儿 tool_call 应有一个合成的 tool_result
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    # Phase 12：富化上下文含工具名和参数
    assert "echo" in content
    assert "interrupted" in content
    assert "text" in content  # 参数应在场


def test_refresh_mcp_tools_applies_diff(monkeypatch) -> None:
    from twinkle.agentserver.agent import ReActAgent
    from twinkle.agentserver.tools.manager import ToolManager
    from twinkle.agentserver.mcp.manager import _ToolDiff
    from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
    from twinkle.agentserver.tools.base import ToolCard
    from twinkle.agentserver.tools.local_function import LocalFunction

    async def _fn(**kwargs): return "ok"
    old_tool = LocalFunction(ToolCard(name="old.x", description="d", parameters={}), _fn)
    new_tool = LocalFunction(ToolCard(name="new.x", description="d", parameters={}), _fn)
    tm = ToolManager()
    tm.register(old_tool)

    agent = ReActAgent(llm=None, store=None, tools=tm, hooks=())   # 无 progressive hook → 不分发

    class _FakeMgr:
        async def refresh_all(self):
            return [_ToolDiff(added=[new_tool], removed=["old.x"])]
    from twinkle.agentserver.mcp import manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda *a, **k: _FakeMgr())

    ctx = HookContext(agent=agent, event=HookEvent.BEFORE_INVOKE,
                      inputs=InvokeInputs(query="", mode=""),
                      session_id="s", request_id="r", extra={})
    asyncio.run(agent._refresh_mcp_tools(ctx))
    names = {t.card.name for t in tm.list()}
    assert "new.x" in names
    assert "old.x" not in names
