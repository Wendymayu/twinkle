"""PlanNode ABC 的测试——带 fallback 与 HookInterrupt 的递归执行节点。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.agentserver.workflow.node import PlanNode


# -- Helpers：用于测试的具体 node 实现 --


class EchoNode(PlanNode):
    """返回其 inputs 的简单 node。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return inputs


class FailingNode(PlanNode):
    """总是抛异常的 node。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise ValueError("boom")


class HookInterruptNode(PlanNode):
    """抛 HookInterrupt 的 node。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise HookInterrupt("approval needed", data={"tool": "rm"})


class CompositeNode(PlanNode):
    """顺序执行 sub-plans 的 parent node。"""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results = []
        for child in self.sub_plans:
            result = await self.execute_subplan(child, inputs)
            results.append(result)
        return results


# -- 测试 --


def test_node_echo():
    """简单 echo node 返回 inputs。"""

    async def _run():
        node = EchoNode(plan_name="echo", instruction="echo inputs")
        result = await node.run({"key": "value"})
        assert result == {"key": "value"}

    asyncio.run(_run())


def test_node_run_with_fallback():
    """异常时调用 fallback callback。"""

    async def _run():
        fallback_called = []

        async def fallback(node: PlanNode, inputs: dict[str, Any], exc: Exception) -> Any:
            fallback_called.append((node.plan_name, str(exc)))
            return {"fallback": True}

        node = FailingNode(
            plan_name="fail",
            instruction="always fails",
        )
        node._fallback_callback = fallback

        result = await node.run({"x": 1})
        assert result == {"fallback": True}
        assert len(fallback_called) == 1
        assert fallback_called[0][0] == "fail"
        assert "boom" in fallback_called[0][1]

    asyncio.run(_run())


def test_node_run_without_fallback_raises():
    """无 fallback 时异常向上抛。"""

    async def _run():
        node = FailingNode(plan_name="fail", instruction="always fails")
        with pytest.raises(ValueError, match="boom"):
            await node.run({"x": 1})

    asyncio.run(_run())


def test_node_hook_interrupt_not_caught_by_fallback():
    """HookInterrupt 不会被 fallback 捕获。"""

    async def _run():
        fallback_called = []

        async def fallback(node: PlanNode, inputs: dict[str, Any], exc: Exception) -> Any:
            fallback_called.append(True)
            return {"fallback": True}

        node = HookInterruptNode(
            plan_name="interrupt",
            instruction="raises interrupt",
        )
        node._fallback_callback = fallback

        with pytest.raises(HookInterrupt):
            await node.run({"x": 1})

        assert fallback_called == []

    asyncio.run(_run())


def test_execute_subplan():
    """Parent 顺序执行 sub-plans。"""

    async def _run():
        child1 = EchoNode(plan_name="c1", instruction="echo")
        child2 = EchoNode(plan_name="c2", instruction="echo")
        parent = CompositeNode(
            plan_name="parent",
            instruction="runs children",
            sub_plans=[child1, child2],
        )
        result = await parent.run({"a": 1})
        assert result == [{"a": 1}, {"a": 1}]

    asyncio.run(_run())


def test_set_runtime_callbacks_propagates():
    """Callbacks 递归传播到 sub_plans。"""

    async def _run():
        def has_tool_fn(name: str) -> bool:
            return name == "test_tool"

        child = EchoNode(plan_name="child", instruction="echo")
        parent = EchoNode(
            plan_name="parent",
            instruction="echo",
            sub_plans=[child],
        )
        parent.set_runtime_callbacks(has_tool=has_tool_fn)

        # Parent 持有 callback
        assert parent.has_tool("test_tool") is True
        assert parent.has_tool("other") is False

        # Child 也持有 callback
        assert child.has_tool("test_tool") is True
        assert child.has_tool("other") is False

    asyncio.run(_run())


def test_call_llm_raises_without_callback():
    """未设置 call_llm callback 时抛 RuntimeError。"""

    async def _run():
        node = EchoNode(plan_name="echo", instruction="echo")
        with pytest.raises(RuntimeError, match="call_llm callback not initialized"):
            await node.call_llm("hello")

    asyncio.run(_run())


def test_has_tool_raises_without_callback():
    """未设置 has_tool callback 时抛 RuntimeError。"""

    node = EchoNode(plan_name="echo", instruction="echo")
    with pytest.raises(RuntimeError, match="has_tool callback not initialized"):
        node.has_tool("anything")


def test_node_repr():
    """repr 包含 plan_name。"""

    node = EchoNode(plan_name="my_node", instruction="echo")
    r = repr(node)
    assert "my_node" in r
    assert "sub_plans=0" in r


def test_subplan_depth_auto_set():
    """Sub-plan 的 depth 自动设为 parent.depth + 1。"""

    grandchild = EchoNode(plan_name="gc", instruction="echo")
    child = EchoNode(plan_name="child", instruction="echo", sub_plans=[grandchild])
    parent = EchoNode(
        plan_name="parent",
        instruction="echo",
        sub_plans=[child],
    )

    assert parent.depth == 0
    assert child.depth == 1
    assert grandchild.depth == 2
