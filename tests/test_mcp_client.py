# tests/test_mcp_client.py
import asyncio
import pytest
from types import SimpleNamespace
from twinkle.agentserver.mcp.client import StdioMcpClient
from twinkle.agentserver.mcp.tool import McpToolCard
from twinkle.agentserver.tools.errors import ToolError


class _FakeSession:
    """fake MCP ClientSession:list_tools/call_tool 返回固定结果。"""
    def __init__(self, tools=None, call_text="tool-output"):
        self._tools = tools or []
        self._call_text = call_text
        self.initialized = False
    async def initialize(self):
        self.initialized = True
    async def list_tools(self):
        return SimpleNamespace(tools=list(self._tools))
    async def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._call_text)])


class _OverrideClient(StdioMcpClient):
    """子类 override _open_session 返回 fake session(不碰真实 mcp SDK)。"""
    def __init__(self, config, fake_session, connect_timeout=5.0, call_timeout=10.0):
        super().__init__(config, connect_timeout, call_timeout)
        self._fake = fake_session
    async def _open_session(self, stack):
        await stack.enter_async_context(_NullCM())  # 注册一个占位 cm 让 aclose 有事可做
        await self._fake.initialize()
        return self._fake


class _NullCM:
    async def __aenter__(self):
        return None
    async def __aexit__(self, *a):
        return False


def _stdio_config(command="npx", args=None):
    from twinkle.config.schema import McpServerConfig
    return McpServerConfig(name="fs", transport="stdio",
                           command=command, args=args or ["-y", "pkg"])


def test_stdio_connect_list_call_disconnect() -> None:
    sess = _FakeSession(
        tools=[SimpleNamespace(name="read", description="d", inputSchema={"type": "object"})],
        call_text="hello")
    c = _OverrideClient(_stdio_config(), sess)
    asyncio.run(c.connect())
    assert sess.initialized
    cards = asyncio.run(c.list_tools())
    assert len(cards) == 1
    assert isinstance(cards[0], McpToolCard)
    assert cards[0].name == "fs.read"
    assert cards[0].server_name == "fs"
    assert asyncio.run(c.call_tool("read", {"p": 1})) == "hello"
    assert sess.last_call == ("read", {"p": 1})
    asyncio.run(c.disconnect())  # no raise


def test_stdio_call_tool_failure_wraps_tool_error() -> None:
    class _BadSession(_FakeSession):
        async def call_tool(self, name, arguments):
            raise ConnectionError("server gone")
    c = _OverrideClient(_stdio_config(), _BadSession())
    asyncio.run(c.connect())
    with pytest.raises(ToolError, match="fs.read"):
        asyncio.run(c.call_tool("read", {}))


def test_stdio_connect_timeout_via_wait_for() -> None:
    class _SlowSession(_FakeSession):
        async def initialize(self):
            await asyncio.sleep(5)  # 超过 connect_timeout
    c = _OverrideClient(_stdio_config(), _SlowSession(), connect_timeout=0.1)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c.connect())


from twinkle.agentserver.mcp.client import StreamableHttpMcpClient


class _OverrideHttp(StreamableHttpMcpClient):
    def __init__(self, config, fake_session, connect_timeout=5.0, call_timeout=10.0, reconnect_attempts=3):
        super().__init__(config, connect_timeout, call_timeout, reconnect_attempts)
        self._fake = fake_session
    async def _open_session(self, stack):
        await stack.enter_async_context(_NullCM())
        await self._fake.initialize()
        return self._fake


def _http_config(url="http://127.0.0.1:8080/mcp"):
    from twinkle.config.schema import McpServerConfig
    return McpServerConfig(name="my", transport="streamable-http", url=url)


def test_http_connect_list_call() -> None:
    sess = _FakeSession(
        tools=[SimpleNamespace(name="search", description="d", inputSchema={"type": "object"})],
        call_text="found")
    c = _OverrideHttp(_http_config(), sess)
    asyncio.run(c.connect())
    cards = asyncio.run(c.list_tools())
    assert cards[0].name == "my.search"
    assert asyncio.run(c.call_tool("search", {"q": "x"})) == "found"


def test_http_call_reconnects_on_retryable_error() -> None:
    attempts = {"n": 0}
    class _Flaky(_FakeSession):
        async def call_tool(self, name, arguments):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("connection closed")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="recovered")])
    c = _OverrideHttp(_http_config(), _Flaky())
    asyncio.run(c.connect())
    # call_tool 挂 with_reconnect:首次 connection closed → 重连 → 成功
    assert asyncio.run(c.call_tool("search", {})) == "recovered"
    assert attempts["n"] == 2
