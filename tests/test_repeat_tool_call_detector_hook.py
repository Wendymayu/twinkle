"""Tests for RepeatToolCallDetectorHook — stable hash, 4-tier detection,
remediation injection, rate limiting, edge-triggered behavior."""

import asyncio
import time

from twinkle.agentserver.hooks.base import HookContext, ModelCallInputs, ToolCallInputs
from twinkle.agentserver.hooks.builtin.repeat_tool_call_detector_hook import (
    RepeatToolCallDetectorHook,
    Severity,
    stable_call_hash,
    stable_result_hash,
)


# --- stable hash tests ---

def test_stable_call_hash_order_independent():
    """Parameter order should not affect hash."""
    h1 = stable_call_hash("read_file", {"path": "a.txt", "offset": 0})
    h2 = stable_call_hash("read_file", {"offset": 0, "path": "a.txt"})
    assert h1 == h2


def test_stable_call_hash_different_args():
    """Different args should produce different hashes."""
    h1 = stable_call_hash("read_file", {"path": "a.txt"})
    h2 = stable_call_hash("read_file", {"path": "b.txt"})
    assert h1 != h2


def test_stable_result_hash_same_content():
    """Same content should produce same hash."""
    h1 = stable_result_hash("result content")
    h2 = stable_result_hash("result content")
    assert h1 == h2


def test_stable_result_hash_different_content():
    """Different content should produce different hashes."""
    h1 = stable_result_hash("result A")
    h2 = stable_result_hash("result B")
    assert h1 != h2


# --- Helper to simulate tool calls ---

def _make_tool_ctx(name, args, result=""):
    """Create a HookContext with ToolCallInputs."""
    return HookContext(
        agent=None, event=None,
        inputs=ToolCallInputs(name=name, args=args, tool_call_id="tc1"),
        session_id=None, request_id=None,
        extra={"_tool_result": result},
    )


def _make_model_ctx(messages):
    """Create a HookContext with ModelCallInputs."""
    return HookContext(
        agent=None, event=None,
        inputs=ModelCallInputs(messages=messages, tools=[]),
        session_id=None, request_id=None,
        extra={},
    )


async def _simulate_tool_call_sequence(hook, calls):
    """Simulate a sequence of tool calls. Each call is (name, args, result)."""
    for name, args, result in calls:
        # before_tool_call
        ctx = _make_tool_ctx(name, args)
        await hook.before_tool_call(ctx)
        # after_tool_call
        ctx.extra["_tool_result"] = result
        await hook.after_tool_call(ctx)


# --- Detection tests ---

def test_detects_repeat_calls_low():
    """Same tool+args appearing >= repeat_warn times -> LOW."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.LOW


def test_detects_pingpong_medium():
    """A-B-A-B alternation >= pingpong_warn -> MEDIUM."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # A-B-A-B pattern (4 alternations)
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
    assert hook._fired_severity == Severity.MEDIUM


def test_detects_trailing_identical_high():
    """Trailing identical (call+outcome) >= loop_block -> HIGH."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.HIGH


def test_detects_critical_loop():
    """Trailing identical >= global_stop -> CRITICAL."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=3)
    calls = [("read_file", {"path": "a.txt"}, "same_result")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.CRITICAL


def test_no_detection_under_threshold():
    """Below all thresholds -> no detection."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=20, global_stop=30)
    calls = [("read_file", {"path": "a.txt"}, "content")] * 2
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity is None


def test_edge_triggered_only_escalates():
    """Severity only rises, never falls — edge-triggered."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # 3 repeats -> LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    assert hook._fired_severity == Severity.LOW

    # 1 more different call — severity should not drop
    calls2 = [("write_file", {"path": "b.txt"}, "ok")]
    asyncio.run(_simulate_tool_call_sequence(hook, calls2))
    # fired_severity stays LOW (not reset, not escalated)
    assert hook._fired_severity == Severity.LOW


# --- Remediation injection tests ---

def test_injects_remediation_message_at_medium():
    """MEDIUM+ severity triggers remediation message injection in before_model_call."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30)
    # Trigger MEDIUM
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

    # Now call before_model_call
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # Check remediation message was injected
    assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_no_injection_below_medium():
    """LOW severity does not trigger remediation injection."""
    hook = RepeatToolCallDetectorHook(repeat_warn=3, pingpong_warn=10, loop_block=20, global_stop=30)
    # Trigger LOW
    calls = [("read_file", {"path": "a.txt"}, "content")] * 3
    asyncio.run(_simulate_tool_call_sequence(hook, calls))

    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))

    # No remediation message
    assert not any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)


def test_remediation_rate_limit():
    """Remediation injection is rate-limited to max_per_minute."""
    hook = RepeatToolCallDetectorHook(
        repeat_warn=10, pingpong_warn=4, loop_block=20, global_stop=30,
        remediation_max_per_minute=2,
    )
    # Trigger MEDIUM
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

    # First 2 injections should succeed
    for _ in range(2):
        msgs = [{"role": "system", "content": "s"}]
        ctx = _make_model_ctx(msgs)
        asyncio.run(hook.before_model_call(ctx))
        assert any("[DETECTION]" in m.get("content", "") for m in ctx.inputs.messages)

    # 3rd should be rate-limited
    msgs = [{"role": "system", "content": "s"}]
    ctx = _make_model_ctx(msgs)
    asyncio.run(hook.before_model_call(ctx))
    # Count how many [DETECTION] messages — should be 0 (rate-limited)
    detection_count = sum(1 for m in ctx.inputs.messages if "[DETECTION]" in m.get("content", ""))
    assert detection_count == 0


def test_different_results_not_counted_as_loop():
    """Same tool+args but different results = progress, not a loop."""
    hook = RepeatToolCallDetectorHook(repeat_warn=10, pingpong_warn=10, loop_block=3, global_stop=30)
    # Same call, different results each time
    calls = [
        ("read_file", {"path": "a.txt"}, "result_1"),
        ("read_file", {"path": "a.txt"}, "result_2"),
        ("read_file", {"path": "a.txt"}, "result_3"),
    ]
    asyncio.run(_simulate_tool_call_sequence(hook, calls))
    # trailing_identical should be 0 (different outcomes), so no HIGH
    assert hook._fired_severity is None or hook._fired_severity <= Severity.LOW
