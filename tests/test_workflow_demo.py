"""集成测试：用 mock LLM 跑 echo-pipeline 示例 workflow。

用 workflows 目录下真实的 root.py 配合 mock LLM，验证完整 engine 管道
（validate → load → bind callbacks → execute）。
"""
import asyncio
import json
import pytest

from twinkle.agentserver.workflow.executor import WorkflowExecutor
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# --- 返回结构化 JSON 的 mock LLM ---

_CALL_COUNT = 0


async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM：gather 阶段返回 JSON 提纲，enrich 阶段返回纯文本。"""
    if "提纲" in prompt:
        return json.dumps({"items": ["要点A", "要点B", "要点C"]}, ensure_ascii=False)
    # enrich 节点 —— 返回一行文本
    return "这是补充说明"


def _make_executor():
    """构造一个带 mock LLM、无真实 tools/subagent 的 executor。"""
    from twinkle.agentserver.workflow.executor import WorkflowExecutor

    class FakeLLM:
        """鸭子类型的 LLMClient —— 仅满足 _call_llm_wrapper 的需要。"""
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
    """加载 echo-pipeline 的 root.py，用 mock LLM 端到端跑一遍。"""
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
    """echo-pipeline 的 root.py 应通过 AST 校验。"""
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
    """echo-pipeline 的 root.py 应能在 sandbox namespace 中加载。"""
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
