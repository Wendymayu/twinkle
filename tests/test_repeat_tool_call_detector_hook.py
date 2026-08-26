"""RepeatToolCallDetectorHook 的测试 —— 稳定 hash、4 档检测、
remediation 注入、rate limiting、edge-triggered 行为。"""

import asyncio

from twinkle.agentserver.hooks.base import HookContext, ModelCallInputs, ToolCallInputs
from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import (
    RepeatToolCallDetectorHook,
    Severity,
    stable_call_hash,
    stable_result_hash,
)


# --- 稳定 hash 测试 ---

def test_stable_call_hash_order_independent():
    """参数顺序不应影响 hash。"""
    h1 = stable_call_hash("read_file", {"path": "a.txt", "offset": 0})
    h2 = stable_call_hash("read_file", {"offset": 0, "path": "a.txt"})
    assert h1 == h2


def test_stable_call_hash_different_args():
    """不同 args 应产生不同 hash。"""
    h1 = stable_call_hash("read_file", {"path": "a.txt"})
    h2 = stable_call_hash("read_file", {"path": "b.txt"})
    assert h1 != h2


def test_stable_result_hash_same_content():
    """相同 content 应产生相同 hash。"""
    h1 = stable_result_hash("result content")
    h2 = stable_result_hash("result content")
    assert h1 == h2


def test_stable_result_hash_different_content():
    """不同 content 应产生不同 hash。"""
    h1 = stable_result_hash("result A")
    h2 = stable_result_hash("result B")
    assert h1 != h2


# --- 模拟 tool 调用的 helper ---

_SESSION_ID = "test-session"

def _make_tool_ctx(name, args, result=""):
    """构造一个带 ToolCallInputs 的 HookContext。"""
    return HookContext(
        agent=None, event=None,
        inputs=ToolCallInputs(name=name, args=args, tool_call_id="tc1"),
        session_id=_SESSION_ID, request_id=None,
        extra={"_tool_result": result},
    )


def _make_model_ctx(messages):
    """构造一个带 ModelCallInputs 的 HookContext。"""
    return HookContext(
        agent=None, event=None,
        inputs=ModelCallInputs(messages=messages, tools=[]),
        session_id=_SESSION_ID, request_id=None,
        extra={},
    )


async def _simulate_tool_call_sequence(hook, calls):
    """模拟一串 tool 调用。每次调用是 (name, args, result)。"""
    for name, args, result in calls:
        # before_tool_call
        ctx = _make_tool_ctx(name, args)
        await hook.before_tool_call(ctx)
        # after_tool_call
        ctx.extra["_tool_result"] = result
        await hook.after_tool_call(ctx)


# --- 检测测试 ---

def test_detects_repeat_calls_low():
    """相同 tool+args 出现 >= repeat_warn 次 -> LOW。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.LOW


def test_detects_pingpong_medium():
    """A-B-A-B 交替 >= pingpong_warn 次 -> MEDIUM。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # A-B-A-B 模式(4 次交替)
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.MEDIUM


def test_detects_trailing_identical_high():
    """尾部相同(调用+结果) >= loop_block -> HIGH。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.HIGH


def test_detects_critical_loop():
    """尾部相同 >= global_stop -> CRITICAL。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=3)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.CRITICAL


def test_no_detection_under_threshold():
    """低于所有阈值 -> 不检测。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 2
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity is None


def test_edge_triggered_only_escalates():
    """severity 只升不降 —— edge-triggered。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # 3 次重复 -> LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.LOW

    # 再来 1 次不同调用 —— severity 不应下降
    calls2 = [("write_file", {"path": "b.txt"}, "ok")]
    asyncio.run(_simulate_tool_call_sequence(hook, calls2))
    # fired_severity 保持 LOW(未重置、未升级)
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity == Severity.LOW


# --- remediation 注入测试 ---

def test_injects_remediation_message_at_medium():
    """MEDIUM 及以上 severity 在 before_model_call 触发 remediation 消息注入。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # 触发 MEDIUM
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    # 现在调用 before_model_call
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # 检查 remediation 消息是否已注入
    assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_no_injection_below_medium():
    """LOW severity 不触发 remediation 注入。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # 触发 LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # 无 remediation 消息
    assert not any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_remediation_rate_limit():
    """remediation 注入受 max_per_minute 限流。"""
    hook = RepeatToolCallDetectorHook(
        repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30,
        remediation_max_per_minute=2,
    )
    # 触发 MEDIUM
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    # 前 2 次注入应成功
    for _ in range(2):
        msgs = [{"role": "system", "content": "s"}]
        ctx = _make_model_ctx(msgs)
        asyncio.run(hook.before_model_call(ctx))
        assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)

    # 第 3 次应被限流
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))
    # 统计 [DETECTION] 消息数 —— 应为 0(被限流)
    detection_count = sum(1 for m in ctx.inputs.messages if "[DETECTION]" in m.get("content", ""))
    assert detection_count == 0


def test_different_results_not_counted_as_loop():
    """相同 tool+args 但结果不同 = 有进展,不是 loop。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    # 相同调用,每次结果不同
    calls = [
        ("read_file", {"path": "a.txt"}, "result_1"),
        ("read_file", {"path": "a.txt"}, "result_2"),
        ("read_file", {"path": "a.txt"}, "result_3"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    # trailing_identical 应为 0(结果不同),故无 HIGH
    state = hook._states.get(_SESSION_ID)
    assert state.fired_severity is None


# --- CRITICAL 硬停测试 ---

def test_critical_requests_force_finish_hard_stop():
    """CRITICAL severity -> before_model_call 请求 force_finish 硬停 loop,
    而非软性 remediation 提醒。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=3)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._states[_SESSION_ID].fired_severity == Severity.CRITICAL

    ctx = _make_model_ctx([{"role": "system", "content": "s"}])
    asyncio.run(hook.before_model_call(ctx))

    ff = ctx.consume_force_finish_request()
    assert ff is not None, "CRITICAL must request force_finish to hard-stop the loop"
    assert "loop" in str(ff.result).lower()


def test_critical_force_finish_bypasses_remediation_rate_limit():
    """CRITICAL 硬停不应受 remediation rate-limiter 约束 ——
    即便 remediation_max_per_minute=0,CRITICAL 仍会 force_finish。"""
    hook = RepeatToolCallDetectorHook(
        repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=3,
        remediation_max_per_minute=0,
    )
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    ctx = _make_model_ctx([{"role": "system", "content": "s"}])
    asyncio.run(hook.before_model_call(ctx))

    ff = ctx.consume_force_finish_request()
    assert ff is not None, "CRITICAL force_finish must bypass remediation rate limit"


def test_medium_still_soft_remediation_not_force_finish():
    """MEDIUM severity 保持软性 remediation 提醒 —— 不得 force_finish。"""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    calls = [
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
        ("read_file", {"path": "a.txt"}, "result_a"),
        ("read_file", {"path": "b.txt"}, "result_b"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._states[_SESSION_ID].fired_severity == Severity.MEDIUM

    ctx = _make_model_ctx([{"role": "system", "content": "s"}])
    asyncio.run(hook.before_model_call(ctx))

    assert ctx.consume_force_finish_request() is None, "MEDIUM must not force_finish"
    assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)
