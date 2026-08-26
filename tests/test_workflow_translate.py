"""集成测试：用 mock LLM 跑 translate workflow。"""
import asyncio
import json
import pytest

from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# --- 按上下文返回翻译的 mock LLM ---

async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM：根据 prompt 内容返回翻译。"""
    if "法语" in prompt and "翻译成" in prompt:
        return "Bonjour le monde"
    if "西班牙语" in prompt and "翻译成" in prompt:
        return "Hola mundo"
    if "审校" in prompt:
        # Review 节点：永远返回最佳翻译（绝不返回"通过"）
        if "法语" in prompt:
            return "Bonjour le monde"
        if "西班牙语" in prompt:
            return "Hola mundo"
        return "通过"
    return "mock"


class FakeLLM:
    """鸭子类型的 LLMClient —— 仅满足 _call_llm_wrapper 的需要。"""
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
    """从 workflows 目录加载 translate 的 root.py。"""
    from pathlib import Path
    root_path = Path.home() / ".twinkle" / "workflows" / "translate" / "root.py"
    if not root_path.exists():
        pytest.skip("translate workflow not installed")
    return root_path.read_text(encoding="utf-8")


def test_translate_workflow_e2e():
    """完整管道：fr + es 翻译带审校。"""
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
    """translate 的 root.py 应通过 AST 校验。"""
    from twinkle.agentserver.workflow.validator import PlanCodeValidator
    plan_code = _load_translate_workflow()
    errors = PlanCodeValidator().validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_translate_workflow_sandbox_loads():
    """translate 的 root.py 应能在 sandbox namespace 中加载。"""
    from twinkle.agentserver.workflow.sandbox import build_namespace
    plan_code = _load_translate_workflow()
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    assert isinstance(root, PlanNode)
    assert root.plan_name == "translate"
    assert len(root.sub_plans) == 2  # fr-pipeline + es-pipeline
