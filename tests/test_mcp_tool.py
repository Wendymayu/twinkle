import asyncio

import pytest
from types import SimpleNamespace

from twinkle.agentserver.mcp.tool import McpTool, McpToolCard, extract_text_content
from twinkle.agentserver.tools.errors import ToolError


def _content(*items):
    return SimpleNamespace(content=list(items))


def test_extract_text_single() -> None:
    r = _content(SimpleNamespace(type="text", text="hello"))
    assert extract_text_content(r) == "hello"


def test_extract_text_last_wins() -> None:
    r = _content(SimpleNamespace(type="text", text="a"),
                 SimpleNamespace(type="text", text="b"))
    assert extract_text_content(r) == "b"


def test_extract_no_text_returns_empty() -> None:
    r = _content(SimpleNamespace(type="image", data=b"x"))
    assert extract_text_content(r) == ""


def test_extract_empty_content() -> None:
    r = _content()
    assert extract_text_content(r) == ""


def test_tool_card_name_prefixed() -> None:
    c = McpToolCard(name="srv.search", server_name="srv",
                    description="d", parameters={"type": "object"})
    assert c.name == "srv.search"
    assert c.server_name == "srv"


class _FakeClient:
    """fake McpClient:call_tool 返回固定值或抛异常。"""
    def __init__(self, call_result="ok", call_exc=None):
        self._result = call_result
        self._exc = call_exc
        self.calls = []

    async def call_tool(self, name, arguments, *, timeout=None):
        self.calls.append((name, arguments))
        if self._exc:
            raise self._exc
        return self._result


def _make_tool(client, server="srv", tool="search"):
    card = McpToolCard(name=f"{server}.{tool}", server_name=server,
                       description="d", parameters={"type": "object"})
    return McpTool(client=client, card=card)


def test_invoke_returns_call_tool_text() -> None:
    c = _FakeClient(call_result="result-text")
    t = _make_tool(c)
    assert asyncio.run(t.invoke({"q": "x"})) == "result-text"
    assert c.calls == [("search", {"q": "x"})]  # 裸 tool 名,去 server 前缀


def test_invoke_wraps_non_tool_error() -> None:
    c = _FakeClient(call_exc=RuntimeError("boom"))
    t = _make_tool(c)
    with pytest.raises(ToolError, match="srv.search: boom"):
        asyncio.run(t.invoke({}))


def test_invoke_propagates_tool_error() -> None:
    c = _FakeClient(call_exc=ToolError("already"))
    t = _make_tool(c)
    with pytest.raises(ToolError, match="already"):
        asyncio.run(t.invoke({}))
