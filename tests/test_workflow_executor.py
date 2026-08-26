"""WorkflowExecutor 的测试 —— validate、sandbox、fallback、timeout、HookInterrupt。"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.agentserver.workflow.executor import (
    ExecutionTimeoutError,
    FallbackLimitExceededError,
    PlanCodeValidationError,
    WorkflowExecutor,
)
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# ---------------------------------------------------------------------------
# 辅助：按 config 构造 executor
# ---------------------------------------------------------------------------

def _make_executor(**config_overrides: Any) -> WorkflowExecutor:
    config = WorkflowConfig(**config_overrides)
    return WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=None,
        config=config,
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_execute_simple_plan():
    """加载并执行一段定义了 root PlanNode 的简单 plan_code。"""
    # PlanNode 已在 sandbox namespace 中 —— 无需 import
    plan_code = '''
class MyNode(PlanNode):
    async def _execute(self, inputs):
        return {"status": "done", "got": inputs.get("x", 0)}

root = MyNode(plan_name="simple", instruction="simple test")
'''
    executor = _make_executor()
    result = asyncio.run(executor.execute_workflow(plan_code, {"x": 42}))
    assert result == {"status": "done", "got": 42}


def test_execute_rejects_bad_syntax():
    """plan_code 中的语法错误会被拒绝。"""
    plan_code = "def broken(\n"
    executor = _make_executor()
    with pytest.raises(PlanCodeValidationError, match="Syntax error"):
        asyncio.run(executor.execute_workflow(plan_code, {}))


def test_execute_rejects_forbidden_import():
    """禁止的 import（如 import os）会被拒绝。"""
    plan_code = "import os\n"
    executor = _make_executor()
    with pytest.raises(PlanCodeValidationError, match="Forbidden import"):
        asyncio.run(executor.execute_workflow(plan_code, {}))


def test_execute_with_fallback():
    """节点失败触发 SubagentExecutor fallback。"""
    plan_code = '''
class FailNode(PlanNode):
    async def _execute(self, inputs):
        raise ValueError("boom")

root = FailNode(plan_name="fail", instruction="will fail")
'''
    # mock 一个 subagent executor
    mock_subagent = AsyncMock()
    mock_subagent.execute_subagent.return_value = type(
        "SubagentResult", (), {"success": True, "result": "fallback_result", "error": None}
    )()

    config = WorkflowConfig(enable_fallback=True, max_fallback_count=3)
    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=mock_subagent,
        config=config,
    )
    result = asyncio.run(executor.execute_workflow(plan_code, {}))
    assert result == "fallback_result"
    mock_subagent.execute_subagent.assert_called_once()


def test_execute_timeout():
    """执行超时会抛 ExecutionTimeoutError。"""
    # asyncio 在 sandbox namespace 中可用
    plan_code = '''
class SlowNode(PlanNode):
    async def _execute(self, inputs):
        await asyncio.sleep(10)
        return {"status": "slow"}

root = SlowNode(plan_name="slow", instruction="slow test")
'''
    executor = _make_executor(execution_timeout=0.1)
    with pytest.raises(ExecutionTimeoutError):
        asyncio.run(executor.execute_workflow(plan_code, {}))


def test_execute_hook_interrupt_propagates():
    """HookInterrupt 不会被 fallback 捕获 —— 一路上抛。"""
    # HookInterrupt 已在 sandbox namespace 中
    plan_code = '''
class InterruptNode(PlanNode):
    async def _execute(self, inputs):
        raise HookInterrupt("HITL approval needed")

root = InterruptNode(plan_name="hitl", instruction="interrupt test")
'''
    mock_subagent = AsyncMock()
    config = WorkflowConfig(enable_fallback=True, max_fallback_count=3)
    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=mock_subagent,
        config=config,
    )
    with pytest.raises(HookInterrupt, match="HITL approval needed"):
        asyncio.run(executor.execute_workflow(plan_code, {}))
    # fallback 不应被调用
    mock_subagent.execute_subagent.assert_not_called()


def test_fallback_skips_infrastructure_errors():
    """基础设施错误（连接/鉴权/限流/超时）绕过 subagent fallback，直接上抛给调用方 ——
    subagent 调用的是同一个 LLM API，必然以同样方式失败，所以用 subagent 重试纯属浪费。

    回归：曾出现 workflow 节点抛 APIConnectionError 时，在 PlanNode 树的每一层
    都 spawn 一个 subagent（2 层 = 2 个 subagent），全部对着同一个挂掉的 API 失败。
    此错误应直达主 agent loop（ReAct），等基础设施恢复后再重跑整个 workflow。
    """
    plan_code = '''
class APIConnectionError(Exception):
    pass

class FailNode(PlanNode):
    async def _execute(self, inputs):
        raise APIConnectionError("Connection error.")

root = FailNode(plan_name="fail", instruction="will fail")
'''
    mock_subagent = AsyncMock()
    config = WorkflowConfig(enable_fallback=True, max_fallback_count=3)
    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=mock_subagent,
        config=config,
    )
    # 基础设施错误必须上抛，不能被吞进 subagent 重试
    with pytest.raises(Exception, match="Connection error"):
        asyncio.run(executor.execute_workflow(plan_code, {}))
    mock_subagent.execute_subagent.assert_not_called()


def test_fallback_skips_timeout_errors():
    """超时类错误绕过 fallback 直接上抛。

    这里用 sandbox 内自定义的 infra 命名异常，是因为 sandbox 受限的 asyncio
    代理直接挡住了 ``asyncio.TimeoutError`` —— 匹配逻辑会沿类继承链走查类名，
    所以任何类名含 infra 关键字的类（此处 "RequestTimeout" → "Timeout"）都会
    被当作基础设施错误，与真实的 asyncio.TimeoutError / openai.APITimeoutError 同等对待。
    """
    plan_code = '''
class RequestTimeout(Exception):
    pass

class TimeoutNode(PlanNode):
    async def _execute(self, inputs):
        raise RequestTimeout("LLM call timed out")

root = TimeoutNode(plan_name="timeout", instruction="will time out")
'''
    mock_subagent = AsyncMock()
    config = WorkflowConfig(enable_fallback=True, max_fallback_count=3)
    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=mock_subagent,
        config=config,
    )
    with pytest.raises(Exception, match="LLM call timed out"):
        asyncio.run(executor.execute_workflow(plan_code, {}))
    mock_subagent.execute_subagent.assert_not_called()
