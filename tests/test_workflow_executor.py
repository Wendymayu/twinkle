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


def test_fallback_skips_infrastructure_errors():
    """Infrastructure errors (connection/auth/rate-limit/timeout) bypass
    subagent fallback and propagate to the caller — a subagent calls the same
    LLM API and would fail identically, so retrying via subagent is pure waste.

    Regression: a workflow node raising APIConnectionError used to spawn a
    subagent at every PlanNode tree level (2 levels = 2 subagents), all failing
    against the same down API. The error should reach the main agent loop
    (ReAct) to retry the whole workflow after infra recovers.
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
    # Infra error must propagate, NOT be swallowed into a subagent retry
    with pytest.raises(Exception, match="Connection error"):
        asyncio.run(executor.execute_workflow(plan_code, {}))
    mock_subagent.execute_subagent.assert_not_called()


def test_fallback_skips_timeout_errors():
    """A timeout-class error bypasses fallback and propagates.

    Uses a sandbox-defined infra-named exception because the sandbox's
    restricted asyncio proxy blocks ``asyncio.TimeoutError`` directly —
    the matching logic walks class-hierarchy names, so any class whose name
    contains an infra keyword (here "RequestTimeout" → "Timeout") is treated
    as infrastructure, same as the real asyncio.TimeoutError / openai.APITimeoutError.
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
