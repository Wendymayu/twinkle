"""Tests for WorkflowExecutor — validate, sandbox, fallback, timeout, HookInterrupt."""
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
# Helper: build executor with config
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
# Tests
# ---------------------------------------------------------------------------

def test_execute_simple_plan():
    """Load and execute a simple plan_code that defines a root PlanNode."""
    # PlanNode is already in the sandbox namespace — no import needed
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
    """Syntax errors in plan_code are rejected."""
    plan_code = "def broken(\n"
    executor = _make_executor()
    with pytest.raises(PlanCodeValidationError, match="Syntax error"):
        asyncio.run(executor.execute_workflow(plan_code, {}))


def test_execute_rejects_forbidden_import():
    """Forbidden imports (e.g., import os) are rejected."""
    plan_code = "import os\n"
    executor = _make_executor()
    with pytest.raises(PlanCodeValidationError, match="Forbidden import"):
        asyncio.run(executor.execute_workflow(plan_code, {}))


def test_execute_with_fallback():
    """Node failure triggers SubagentExecutor fallback."""
    plan_code = '''
class FailNode(PlanNode):
    async def _execute(self, inputs):
        raise ValueError("boom")

root = FailNode(plan_name="fail", instruction="will fail")
'''
    # Mock subagent executor
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
    """Execution timeout raises ExecutionTimeoutError."""
    # asyncio is available in the sandbox namespace
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
    """HookInterrupt is not caught by fallback — it propagates up."""
    # HookInterrupt is already in the sandbox namespace
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
    # Fallback should NOT have been called
    mock_subagent.execute_subagent.assert_not_called()
