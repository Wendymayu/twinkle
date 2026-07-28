from twinkle.agentserver.tools.subagent_executor.models import (
    EXCLUDED_TOOLS,
    SoftTimeoutError,
    SubagentResult,
    SubagentTaskSpec,
)


def test_task_spec_autogenerates_task_id():
    t = SubagentTaskSpec(objective="do X")
    assert t.task_id.startswith("subagent_")
    assert t.role_id == "MainAgent"
    assert t.objective == "do X"
    assert t.prompt == ""
    assert t.model_name == ""


def test_two_specs_have_different_ids():
    a = SubagentTaskSpec(objective="a")
    b = SubagentTaskSpec(objective="b")
    assert a.task_id != b.task_id


def test_excluded_tools_set():
    assert "spawn_subagent" in EXCLUDED_TOOLS
    assert "write_memory" in EXCLUDED_TOOLS
    assert "edit_memory" in EXCLUDED_TOOLS
    assert "memory_search" not in EXCLUDED_TOOLS
    assert "read_memory" not in EXCLUDED_TOOLS


def test_result_success_and_failure():
    ok = SubagentResult(success=True, task_id="t1", role_id="MainAgent", result="answer")
    assert ok.success is True
    assert ok.result == "answer"
    fail = SubagentResult(success=False, task_id="t2", role_id="MainAgent", error="boom")
    assert fail.success is False
    assert fail.error == "boom"


def test_soft_timeout_error_is_exception():
    assert issubclass(SoftTimeoutError, Exception)
