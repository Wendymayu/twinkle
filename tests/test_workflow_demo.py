"""Integration test: echo-pipeline demo workflow with mock LLM.

Verifies the full engine pipeline (validate → load → bind callbacks → execute)
using the actual root.py from the workflows directory, with a mock LLM.
"""
import asyncio
import json
import pytest

from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# --- Mock LLM that returns structured JSON ---

_CALL_COUNT = 0


async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM: returns JSON outline for gather, plain text for enrich."""
    if "提纲" in prompt:
        return json.dumps({"items": ["要点A", "要点B", "要点C"]}, ensure_ascii=False)
    # enrich node — return a one-liner
    return "这是补充说明"


def _make_executor():
    """Build an executor with mock LLM and no real tools/subagent."""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor

    class FakeLLM:
        """Duck-type LLMClient — just enough for _call_llm_wrapper."""
        async def stream(self, messages, tools=None):
            from twinkle.agentserver.llm_client import TextDelta
            prompt = messages[-1]["content"]
            result = await _mock_call_llm(prompt)
            yield TextDelta(content=result)

    return WorkflowExecutor(
        llm=FakeLLM(),
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )


def test_echo_pipeline_with_mock_llm():
    """Load the echo-pipeline root.py and run it end-to-end with mock LLM."""
    from pathlib import Path

    root_path = Path.home() / ".twinkle" / "workflows" / "echo-pipeline" / "root.py"
    if not root_path.exists():
        pytest.skip("echo-pipeline workflow not installed")

    plan_code = root_path.read_text(encoding="utf-8")
    executor = _make_executor()

    result = asyncio.run(executor.execute_workflow(plan_code, {"topic": "AI测试"}))

    assert result["node"] == "merge"
    assert result["status"] == "ok"
    assert result["topic"] == "AI测试"
    assert len(result["outline"]) == 3
    assert result["outline"][0]["item"] == "要点A"
    assert "detail" in result["outline"][0]


def test_echo_pipeline_plan_code_validates():
    """The echo-pipeline root.py should pass AST validation."""
    from pathlib import Path
    from twinkle.agentserver.workflow.validator import PlanCodeValidator

    root_path = Path.home() / ".twinkle" / "workflows" / "echo-pipeline" / "root.py"
    if not root_path.exists():
        pytest.skip("echo-pipeline workflow not installed")

    plan_code = root_path.read_text(encoding="utf-8")
    validator = PlanCodeValidator()
    errors = validator.validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_echo_pipeline_sandbox_loads():
    """The echo-pipeline root.py should load in the sandbox namespace."""
    from pathlib import Path
    from twinkle.agentserver.workflow.sandbox import build_namespace

    root_path = Path.home() / ".twinkle" / "workflows" / "echo-pipeline" / "root.py"
    if not root_path.exists():
        pytest.skip("echo-pipeline workflow not installed")

    plan_code = root_path.read_text(encoding="utf-8")
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    assert isinstance(root, PlanNode)
    assert root.plan_name == "echo-pipeline"
    assert len(root.sub_plans) == 3
