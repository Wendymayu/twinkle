"""End-to-end integration tests for the Workflow engine.

Covers: 3-layer PlanNode tree, fallback via SubagentExecutor, HookInterrupt
bypassing fallback, and FallbackLimitExceededError.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.agentserver.workflow.executor import (
    FallbackLimitExceededError,
    WorkflowExecutor,
)
from twinkle.agentserver.workflow.node import PlanNode
from twinkle.config.schema import WorkflowConfig


# ---------------------------------------------------------------------------
# Custom PlanNode subclasses for tests
# ---------------------------------------------------------------------------


class LeafNode(PlanNode):
    """Doubles inputs['value'] and returns as leaf_result."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"leaf_result": inputs["value"] * 2}


class BranchNode(PlanNode):
    """Executes two leaf sub-plans, stores results in inputs[sub.plan_name]."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results: dict[str, Any] = {}
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            results[sub.plan_name] = result
        return results


class RootNode(PlanNode):
    """Orchestrates branch sub-plans, updates inputs with sub-plan results."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results: dict[str, Any] = {}
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            results[sub.plan_name] = result
        return results


class FailNode(PlanNode):
    """Always raises RuntimeError."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise RuntimeError("deliberate failure")


class InterruptNode(PlanNode):
    """Always raises HookInterrupt."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise HookInterrupt("HITL approval needed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSubagentExecutor:
    """Minimal fake that returns a successful SubagentResult with degraded status."""

    async def execute_subagent(
        self,
        task: Any,
        parent_session_id: str = "",
        parent_request_id: str = "",
    ) -> Any:
        return type(
            "SubagentResult",
            (),
            {
                "success": True,
                "result": {"status": "degraded", "result": "fallback result"},
                "error": None,
            },
        )()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_three_layer_tree():
    """3-layer tree: root -> branch -> leaf, inputs correctly passed.

    LeafNode doubles inputs["value"]; BranchNode executes two leaf sub-plans
    and stores results keyed by plan_name; RootNode orchestrates.
    """
    leaf1 = LeafNode(plan_name="leaf1", instruction="double value")
    leaf2 = LeafNode(plan_name="leaf2", instruction="double value")
    branch = BranchNode(
        plan_name="branch",
        instruction="run leaves",
        sub_plans=[leaf1, leaf2],
    )
    root = RootNode(
        plan_name="root",
        instruction="orchestrate",
        sub_plans=[branch],
    )

    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )

    # Bind callbacks manually (as the brief specifies)
    root.set_runtime_callbacks(
        has_tool=executor._has_tool_wrapper,
        call_tool=executor._call_tool_wrapper,
        call_llm=executor._call_llm_wrapper,
        fallback=executor._fallback_wrapper,
        extract_json=executor._extract_json_wrapper,
    )

    result = asyncio.run(root.run({"value": 5}))

    assert result["branch"]["leaf1"] == {"leaf_result": 10}
    assert result["branch"]["leaf2"] == {"leaf_result": 10}


def test_fallback_with_subagent_executor():
    """Node failure triggers SubagentExecutor fallback."""
    root = FailNode(plan_name="fail", instruction="will fail")

    fake_subagent = FakeSubagentExecutor()

    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=fake_subagent,
        config=WorkflowConfig(enable_fallback=True, max_fallback_count=3),
    )

    root.set_runtime_callbacks(
        has_tool=executor._has_tool_wrapper,
        call_tool=executor._call_tool_wrapper,
        call_llm=executor._call_llm_wrapper,
        fallback=executor._fallback_wrapper,
        extract_json=executor._extract_json_wrapper,
    )

    result = asyncio.run(root.run({}))
    assert result["status"] == "degraded"
    assert result["result"] == "fallback result"


def test_hook_interrupt_not_caught():
    """HookInterrupt never caught by fallback — it propagates up."""
    root = InterruptNode(plan_name="hitl", instruction="interrupt test")

    fake_subagent = FakeSubagentExecutor()

    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=fake_subagent,
        config=WorkflowConfig(enable_fallback=True, max_fallback_count=3),
    )

    root.set_runtime_callbacks(
        has_tool=executor._has_tool_wrapper,
        call_tool=executor._call_tool_wrapper,
        call_llm=executor._call_llm_wrapper,
        fallback=executor._fallback_wrapper,
        extract_json=executor._extract_json_wrapper,
    )

    with pytest.raises(HookInterrupt, match="HITL approval needed"):
        asyncio.run(root.run({}))


def test_fallback_limit():
    """Exceeding max_fallback_count raises FallbackLimitExceededError.

    Two FailNode sub-plans, FakeSubagentExecutor, max_fallback_count=1.
    First fallback succeeds, second exceeds limit.
    """
    leaf1 = FailNode(plan_name="fail1", instruction="fail 1")
    leaf2 = FailNode(plan_name="fail2", instruction="fail 2")

    root = RootNode(
        plan_name="root",
        instruction="orchestrate",
        sub_plans=[leaf1, leaf2],
    )

    fake_subagent = FakeSubagentExecutor()

    executor = WorkflowExecutor(
        llm=None,
        tools=None,
        subagent_executor=fake_subagent,
        config=WorkflowConfig(enable_fallback=True, max_fallback_count=1),
    )

    root.set_runtime_callbacks(
        has_tool=executor._has_tool_wrapper,
        call_tool=executor._call_tool_wrapper,
        call_llm=executor._call_llm_wrapper,
        fallback=executor._fallback_wrapper,
        extract_json=executor._extract_json_wrapper,
    )

    # Reset fallback count as executor would do
    executor._fallback_count = 0

    with pytest.raises(FallbackLimitExceededError):
        asyncio.run(root.run({}))
