"""Workflow 引擎的端到端集成测试。

覆盖：3 层 PlanNode 树、经 SubagentExecutor 的 fallback、HookInterrupt
绕过 fallback、以及 FallbackLimitExceededError。
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
# 测试用自定义 PlanNode 子类
# ---------------------------------------------------------------------------


class LeafNode(PlanNode):
    """把 inputs['value'] 翻倍，作为 leaf_result 返回。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"leaf_result": inputs["value"] * 2}


class BranchNode(PlanNode):
    """执行两个 leaf sub-plan，按 sub.plan_name 收集结果到本地 dict 后返回。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results: dict[str, Any] = {}
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            results[sub.plan_name] = result
        return results


class RootNode(PlanNode):
    """编排 branch sub-plan，按 sub.plan_name 收集结果到本地 dict 后返回。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results: dict[str, Any] = {}
        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            results[sub.plan_name] = result
        return results


class FailNode(PlanNode):
    """总是抛出 RuntimeError。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise RuntimeError("deliberate failure")


class InterruptNode(PlanNode):
    """总是抛出 HookInterrupt。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise HookInterrupt("HITL approval needed")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


class FakeSubagentExecutor:
    """最小化的 fake，返回成功但状态为 degraded 的 SubagentResult。"""

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
# 测试
# ---------------------------------------------------------------------------


def test_three_layer_tree():
    """3 层树：root -> branch -> leaf，inputs 正确传递。

    LeafNode 把 inputs["value"] 翻倍；BranchNode 执行两个 leaf sub-plan
    并按 plan_name 收集结果；RootNode 负责编排。
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

    # 手动绑定 callbacks（按 brief 要求）
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
    """节点失败触发 SubagentExecutor fallback。"""
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
    """HookInterrupt 不会被 fallback 捕获 —— 一路上抛。"""
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
    """超过 max_fallback_count 会抛 FallbackLimitExceededError。

    两个 FailNode sub-plan，FakeSubagentExecutor，max_fallback_count=1。
    第一次 fallback 成功，第二次超限。
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

    # 重置 fallback 计数，模拟 executor 的行为
    executor._fallback_count = 0

    with pytest.raises(FallbackLimitExceededError):
        asyncio.run(root.run({}))
