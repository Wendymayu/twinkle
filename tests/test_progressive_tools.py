# tests/test_progressive_tools.py
"""ToolsSearchTool: 按 deferred 工具注册名精确(大小写不敏感)查 schema,返回 JSON 字符串。"""
import asyncio
import json

import pytest

from twinkle.agentserver.tools.builtin.progressive_tools import ToolsSearchTool, InvokeToolTool, META_NAMES
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


def _tm_with_tools():
    @tool
    async def read_file(path: str) -> str:
        """read a file"""
        return f"content:{path}"

    @tool
    async def mcp_query(sql: str) -> str:
        """query db"""
        return f"rows:{sql}"

    m = ToolManager()
    m.register(read_file)
    m.register(mcp_query)
    return m


_EAGER = {"read_file", "tools_search", "invoke_tool"}


def test_tools_search_finds_deferred_by_exact_name():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "mcp_query"})))
    assert res["success"] is True
    assert len(res["matches"]) == 1
    assert res["matches"][0]["name"] == "mcp_query"
    assert "input_schema" in res["matches"][0]
    assert res["count"] == 1
    assert "input_schema" in res["message"]  # guidance points to invoke_tool via schema


def test_tools_search_case_insensitive():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "MCP_QUERY"})))
    assert res["success"] is True


def test_tools_search_miss_returns_guidance_message():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "nope"})))
    assert res["success"] is False
    assert res["matches"] == []
    assert "导航列表" in res["message"]


def test_tools_search_excludes_eager_tools():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": "read_file"})))
    assert res["success"] is False  # eager 不在 deferred,搜不到


def test_tools_search_empty_name_returns_required_message():
    m = _tm_with_tools()
    t = ToolsSearchTool(m, _EAGER)
    res = json.loads(asyncio.run(t.invoke({"tool_name": ""})))
    assert res["success"] is False
    assert res["matches"] == []
    assert "tool_name is required" in res["message"]

    res2 = json.loads(asyncio.run(t.invoke({"tool_name": "   "})))
    assert res2["success"] is False
    assert "tool_name is required" in res2["message"]

    res3 = json.loads(asyncio.run(t.invoke({})))  # missing key entirely
    assert res3["success"] is False
    assert "tool_name is required" in res3["message"]


def test_meta_names_constant():
    assert META_NAMES == frozenset({"tools_search", "invoke_tool"})


def test_invoke_tool_runs_deferred_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    # mcp_query 是 deferred,转调 tm.execute
    result = asyncio.run(inv.invoke({"tool_name": "mcp_query",
                                     "arguments": {"sql": "SELECT 1"}}))
    assert result == "rows:SELECT 1"


def test_invoke_tool_rejects_eager_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    res = json.loads(asyncio.run(inv.invoke({"tool_name": "read_file",
                                            "arguments": {"path": "x"}})))
    assert res["success"] is False
    assert "不是按需可见工具" in res["error"]


def test_invoke_tool_rejects_unknown_tool():
    m = _tm_with_tools()
    inv = InvokeToolTool(m, _EAGER)
    res = json.loads(asyncio.run(inv.invoke({"tool_name": "nope",
                                            "arguments": {}})))
    assert res["success"] is False
    assert "不是按需可见工具" in res["error"]  # same contract as eager-reject


def test_invoke_tool_propagates_deferred_tool_exception():
    """#7: meta-tool 异常传播到 agent loop(不吞成 JSON),保 RetryHook/ON_TOOL_EXCEPTION。
    实现故意不 try/except(对齐 catch-specific-then-propagate 约定);本测试锁定该行为。"""
    @tool
    async def raising_tool(x: str) -> str:
        """raises on purpose"""
        raise RuntimeError("boom")
    m = _tm_with_tools()
    m.register(raising_tool)   # raising_tool 不在 _EAGER → deferred
    inv = InvokeToolTool(m, _EAGER)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(inv.invoke({"tool_name": "raising_tool", "arguments": {"x": "1"}}))
