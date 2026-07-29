from twinkle.agentserver.tools.builtin.subagent.models import (
    EXCLUDED_TOOLS,
    SoftTimeoutError,
    SubagentResult,
)


def test_excluded_tools_set():
    assert "spawn_subagent" in EXCLUDED_TOOLS
    assert "write_memory" in EXCLUDED_TOOLS
    assert "edit_memory" in EXCLUDED_TOOLS
    assert "memory_search" not in EXCLUDED_TOOLS
    assert "read_memory" not in EXCLUDED_TOOLS


def test_result_success_and_failure():
    ok = SubagentResult(success=True, result="answer")
    assert ok.success is True
    assert ok.result == "answer"
    fail = SubagentResult(success=False, error="boom")
    assert fail.success is False
    assert fail.error == "boom"


def test_soft_timeout_error_is_exception():
    assert issubclass(SoftTimeoutError, Exception)
