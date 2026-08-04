"""Tests for PlanNode ABC — recursive execution node with fallback and HookInterrupt."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from twinkle.agentserver.hooks.base import HookInterrupt
from twinkle.agentserver.workflow.node import PlanNode


# -- Helpers: concrete node implementations for testing --


class EchoNode(PlanNode):
    """Simple node that returns its inputs."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return inputs


class FailingNode(PlanNode):
    """Node that always raises."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise ValueError("boom")


class HookInterruptNode(PlanNode):
    """Node that raises HookInterrupt."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise HookInterrupt("approval needed", data={"tool": "rm"})


class CompositeNode(PlanNode):
    """Parent node that executes sub-plans sequentially."""

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        results = []
        for child in self.sub_plans:
            result = await self.execute_subplan(child, inputs)
            results.append(result)
        return results


# -- Tests --


def test_node_echo():
    """Simple echo node returns inputs."""

    async def _run():
        node = EchoNode(plan_name="echo", instruction="echo inputs")
        result = await node.run({"key": "value"})
        assert result == {"key": "value"}

    asyncio.run(_run())


def test_node_run_with_fallback():
    """Fallback callback called on exception."""

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
    """Without fallback, exception propagates."""

    async def _run():
        node = FailingNode(plan_name="fail", instruction="always fails")
        with pytest.raises(ValueError, match="boom"):
            await node.run({"x": 1})

    asyncio.run(_run())


def test_node_hook_interrupt_not_caught_by_fallback():
    """HookInterrupt never caught by fallback."""

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
    """Parent executes sub-plans sequentially."""

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
    """Callbacks recursively propagated to sub_plans."""

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

        # Parent has the callback
        assert parent.has_tool("test_tool") is True
        assert parent.has_tool("other") is False

        # Child also has the callback
        assert child.has_tool("test_tool") is True
        assert child.has_tool("other") is False

    asyncio.run(_run())


def test_call_llm_raises_without_callback():
    """RuntimeError if call_llm callback not set."""

    async def _run():
        node = EchoNode(plan_name="echo", instruction="echo")
        with pytest.raises(RuntimeError, match="call_llm callback not initialized"):
            await node.call_llm("hello")

    asyncio.run(_run())


def test_has_tool_raises_without_callback():
    """RuntimeError if has_tool callback not set."""

    node = EchoNode(plan_name="echo", instruction="echo")
    with pytest.raises(RuntimeError, match="has_tool callback not initialized"):
        node.has_tool("anything")


def test_node_repr():
    """repr contains plan_name."""

    node = EchoNode(plan_name="my_node", instruction="echo")
    r = repr(node)
    assert "my_node" in r
    assert "sub_plans=0" in r


def test_subplan_depth_auto_set():
    """Sub-plan depth auto-set to parent.depth + 1."""

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
