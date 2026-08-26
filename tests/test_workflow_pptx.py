"""集成测试：用 mock LLM 跑 ppt workflow。"""
import asyncio
import json
import pytest
from pathlib import Path


# 内置 workflow 位于 engine 包内
# twinkle/agentserver/workflow/ppt/root.py（启动时由 ensure_workspace_dir 播种到
# <WORKSPACE>/workflows/ppt/root.py）。
_BUNDLED_ROOT_PY = (
    Path(__file__).resolve().parent.parent
    / "twinkle" / "agentserver" / "workflow" / "ppt" / "root.py"
)


# --- 按上下文返回响应的 mock LLM ---

async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM：根据 prompt 内容返回结构化响应。"""
    import json as _json
    import re as _re

    if "提取PPT演示主题" in prompt:
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
    """鸭子类型的 LLMClient —— 仅满足 _call_llm_wrapper 的需要。"""
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
    """从内置包路径加载 ppt root.py
    （twinkle/agentserver/workflow/ppt/root.py）。启动时播种到
    <WORKSPACE>/workflows/ppt/root.py；测试直接读内置源码，这样在全新机器 / CI
    上无需安装即可运行。"""
    assert _BUNDLED_ROOT_PY.is_file(), f"bundled workflow missing: {_BUNDLED_ROOT_PY}"
    return _BUNDLED_ROOT_PY.read_text(encoding="utf-8")


def test_seed_bundled_workflows_copies_ppt(tmp_path):
    """ensure_workspace_dir 的 workflow seeder 会把内置 ppt workflow
    （twinkle/agentserver/workflow/ppt/）复制到 <ws>/workflows/ppt/。"""
    from twinkle.workspace import _seed_bundled_workflows
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    # 全新目标：ppt 从内置包 workflow/ppt/ 复制过来。
    _seed_bundled_workflows(str(workflows_dir))
    seeded = workflows_dir / "ppt" / "root.py"
    assert seeded.is_file()
    assert seeded.read_text(encoding="utf-8") == _BUNDLED_ROOT_PY.read_text(encoding="utf-8")


def test_seed_bundled_workflows_skips_existing(tmp_path):
    """若 <ws>/workflows/ppt/ 已存在，seeder 不得覆盖（保留用户改动）。"""
    from twinkle.workspace import _seed_bundled_workflows
    workflows_dir = tmp_path / "workflows"
    (workflows_dir / "ppt").mkdir(parents=True)
    user_edit = "# my custom workflow\n"
    (workflows_dir / "ppt" / "root.py").write_text(user_edit, encoding="utf-8")
    _seed_bundled_workflows(str(workflows_dir))
    assert (workflows_dir / "ppt" / "root.py").read_text(encoding="utf-8") == user_edit


def test_pptx_workflow_validates():
    """ppt 的 root.py 应通过 AST 校验。"""
    from twinkle.agentserver.workflow.validator import PlanCodeValidator
    plan_code = _load_pptx_workflow()
    errors = PlanCodeValidator().validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_pptx_workflow_sandbox_loads():
    """ppt 的 root.py 应能在 sandbox namespace 中加载。"""
    from twinkle.agentserver.workflow.sandbox import build_namespace
    plan_code = _load_pptx_workflow()
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    from twinkle.agentserver.workflow.node import PlanNode
    assert isinstance(root, PlanNode)
    assert root.plan_name == "ppt"
    assert len(root.sub_plans) == 7


def test_pptx_workflow_e2e_no_export():
    """完整管道 —— 用 mock LLM 生成内容，用真实 tools 做文件 I/O 和导出。"""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    # 接上真实 ToolManager，让 command_exec + write_file 可用
    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT"}))

    assert result["node"] == "delivery"
    assert result["status"] == "ok"
    assert "人工智能" in result.get("topic", "")
    assert "pptx_path" in result


def test_pptx_workflow_extract_json():
    """验证各阶段在不同输入下都能产出正确结果。"""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT，面向技术团队"}))

    assert isinstance(result, dict)
    assert result.get("status") == "ok"
    print(f"Workflow result: {json.dumps(result, ensure_ascii=False, default=str)[:500]}")


def test_pptx_workflow_spec_mode():
    """Spec 模式：agent 传入结构化输入（topic/audience/page_count/outline），
    不含 'text' 键。Workflow 必须遵从这些输入、跳过 LLM 抽取，并产出
    非空 topic + 真实（非 dotfile）的 pptx 文件名。

    这是空 topic / '.pptx' 隐藏 dotfile bug 的回归测试。
    """
    plan_code = _load_pptx_workflow()
    executor = _make_executor()
    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    spec_inputs = {
        "topic": "AI Agent 从入门到精通",
        "audience": "技术团队",
        "page_count": 4,
        "style": "tech-minimal",
        "outline": [
            "封面：AI Agent 从入门到精通",
            "什么是 AI Agent：定义、核心能力、自主性",
            "Agent 架构：感知-规划-记忆-行动",
            "问答 Q&A：核心要点回顾",
        ],
    }
    result = asyncio.run(executor.execute_workflow(plan_code, spec_inputs))

    assert result["status"] == "ok"
    assert result["topic"] == "AI Agent 从入门到精通", f"topic lost: {result.get('topic')!r}"
    pptx_path = result["pptx_path"]
    assert pptx_path, "pptx_path missing"
    # 文件名不得为空或隐藏 dotfile —— .pptx 前必须有真实名字
    import os as _os
    basename = _os.path.basename(pptx_path)
    assert basename not in (".pptx", ""), f"empty/hidden filename: {pptx_path!r}"
    assert basename.endswith(".pptx")
    print(f"Spec-mode result: topic={result['topic']!r} pptx_path={pptx_path!r} pages={result.get('page_count')}")


def test_pptx_workflow_exported_file_exists():
    """端到端：.pptx 文件确实写到了磁盘且非空。"""
    import os as _os
    plan_code = _load_pptx_workflow()
    executor = _make_executor()
    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    spec_inputs = {
        "topic": "时间管理",
        "page_count": 3,
        "outline": [
            "封面：时间管理",
            "核心原则：要事第一",
            "总结：回顾要点",
        ],
    }
    result = asyncio.run(executor.execute_workflow(plan_code, spec_inputs))
    assert result["status"] == "ok"

    from twinkle.config import WORKSPACE_DIR
    abs_path = _os.path.join(WORKSPACE_DIR, result["pptx_path"])
    assert _os.path.exists(abs_path), f"PPTX not written: {abs_path}"
    assert _os.path.getsize(abs_path) > 0, f"PPTX empty: {abs_path}"
    print(f"Exported PPTX verified: {abs_path} ({_os.path.getsize(abs_path)} bytes)")
