"""Integration test: translate workflow with mock LLM."""
import asyncio
import json
import pytest

from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# --- Mock LLM that returns context-aware translations ---

async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM: returns translations based on prompt content."""
    if "法语" in prompt and "翻译成" in prompt:
        return "Bonjour le monde"
    if "西班牙语" in prompt and "翻译成" in prompt:
        return "Hola mundo"
    if "审校" in prompt:
        # Review node: always return the best translation (never "通过")
        if "法语" in prompt:
            return "Bonjour le monde"
        if "西班牙语" in prompt:
            return "Hola mundo"
        return "通过"
    return "mock"


class FakeLLM:
    """Duck-type LLMClient — just enough for _call_llm_wrapper."""
    async def stream(self, messages, tools=None):
        from twinkle.agentserver.llm_client import TextDelta
        prompt = messages[-1]["content"]
        result = await _mock_call_llm(prompt)
        yield TextDelta(content=result)


def _make_executor():
    return WorkflowExecutor(
        llm=FakeLLM(),
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )


def _load_translate_workflow():
    """Load the translate root.py from the workflows directory."""
    from pathlib import Path
    root_path = Path.home() / ".twinkle" / "workflows" / "translate" / "root.py"
    if not root_path.exists():
        pytest.skip("translate workflow not installed")
    return root_path.read_text(encoding="utf-8")


def test_translate_workflow_e2e():
    """Full pipeline: fr + es translation with review."""
    plan_code = _load_translate_workflow()
    executor = _make_executor()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "你好世界"}))

    assert result["node"] == "merge"
    assert result["status"] == "ok"
    assert result["source_text"] == "你好世界"
    assert "fr" in result["translations"]
    assert "es" in result["translations"]
    assert "Bonjour" in result["translations"]["fr"]
    assert "Hola" in result["translations"]["es"]


def test_translate_workflow_validates():
    """The translate root.py should pass AST validation."""
    from twinkle.agentserver.workflow.validator import PlanCodeValidator
    plan_code = _load_translate_workflow()
    errors = PlanCodeValidator().validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_translate_workflow_sandbox_loads():
    """The translate root.py should load in the sandbox namespace."""
    from twinkle.agentserver.workflow.sandbox import build_namespace
    plan_code = _load_translate_workflow()
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    assert isinstance(root, PlanNode)
    assert root.plan_name == "translate"
    assert len(root.sub_plans) == 2  # fr-pipeline + es-pipeline
