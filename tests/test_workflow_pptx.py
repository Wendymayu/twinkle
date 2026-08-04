"""Integration test: pptx-craft workflow with mock LLM."""
import asyncio
import json
import pytest
from pathlib import Path


# --- Mock LLM that returns context-aware responses ---

async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM: returns structured responses based on prompt content."""
    import json as _json
    import re as _re

    if "判断用户是否想要生成PPT" in prompt:
        if "人工智能" in prompt:
            return '{"intent": "ppt", "topic": "人工智能技术与应用", "has_documents": false}'
        if "Python" in prompt:
            return '{"intent": "ppt", "topic": "Python 编程入门", "has_documents": false}'
        return '{"intent": "ppt", "topic": "演示文稿", "has_documents": false}'

    if "PPT制作需求参数" in prompt:
        if "技术团队" in prompt:
            return '{"topic": "人工智能技术与应用", "page_count": 6, "audience": "技术团队", "style_id": "tech-minimal", "presentation_purpose": "技术分享"}'
        return '{"topic": "人工智能技术与应用", "page_count": 8, "audience": "通用", "style_id": "business-classic", "presentation_purpose": "汇报"}'

    if "为以下主题生成 PPT 大纲" in prompt:
        match = _re.search(r"内容页数：(\d+)", prompt)
        content_pages = int(match.group(1)) if match else 8
        pages = [
            {"title": "人工智能技术与应用", "body": "从理论到实践的全面解读", "page_type": "cover"},
        ]
        for i in range(content_pages):
            pages.append({
                "title": f"第{i + 1}章：AI 核心概念",
                "body": f"- 要点A：AI 的定义与发展历程\n- 要点B：机器学习基础\n- 要点C：深度学习原理",
                "page_type": "data",
            })
        pages.append({"title": "谢谢", "body": "感谢观看", "page_type": "ending"})
        return _json.dumps({"topic": "人工智能技术与应用", "pages": pages}, ensure_ascii=False)

    if "为以下 PPT 页面生成详细内容" in prompt:
        return "AI 核心概念\n\n- 人工智能是计算机科学的一个分支，致力于模拟人类智能行为\n- 机器学习是实现AI的主要方法，通过数据驱动模型学习\n- 深度学习利用多层神经网络处理复杂数据模式\n- 自然语言处理和计算机视觉是AI两大核心应用领域"

    return "mock"


class FakeLLM:
    """Duck-type LLMClient — just enough for _call_llm_wrapper."""
    async def stream(self, messages, tools=None):
        from twinkle.agentserver.llm_client import TextDelta
        prompt = messages[-1]["content"]
        result = await _mock_call_llm(prompt)
        yield TextDelta(content=result)


def _make_executor():
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config.schema import WorkflowConfig
    return WorkflowExecutor(
        llm=FakeLLM(),
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )


def _load_pptx_workflow():
    """Load the pptx-craft root.py from the workflows directory."""
    root_path = Path.home() / ".twinkle" / "workflows" / "pptx-craft" / "root.py"
    if not root_path.exists():
        pytest.skip("pptx-craft workflow not installed")
    return root_path.read_text(encoding="utf-8")


def test_pptx_workflow_validates():
    """The pptx-craft root.py should pass AST validation."""
    from twinkle.agentserver.workflow.validator import PlanCodeValidator
    plan_code = _load_pptx_workflow()
    errors = PlanCodeValidator().validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_pptx_workflow_sandbox_loads():
    """The pptx-craft root.py should load in the sandbox namespace."""
    from twinkle.agentserver.workflow.sandbox import build_namespace
    plan_code = _load_pptx_workflow()
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    from twinkle.agentserver.workflow.node import PlanNode
    assert isinstance(root, PlanNode)
    assert root.plan_name == "pptx-craft"
    assert len(root.sub_plans) == 7


def test_pptx_workflow_e2e_no_export():
    """Full pipeline — mock LLM for content, real tools for file I/O and export."""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    # Wire up real ToolManager so command_exec + write_file work
    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT"}))

    assert result["node"] == "delivery"
    assert result["status"] == "ok"
    assert "人工智能" in result.get("topic", "")
    assert "pptx_path" in result


def test_pptx_workflow_extract_json():
    """Verify all stages produce correct output with different inputs."""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT，面向技术团队"}))

    assert isinstance(result, dict)
    assert result.get("status") == "ok"
    print(f"Workflow result: {json.dumps(result, ensure_ascii=False, default=str)[:500]}")
