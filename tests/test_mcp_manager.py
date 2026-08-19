# tests/test_mcp_manager.py
import asyncio
import pytest
from twinkle.agentserver.mcp.manager import McpManager, get_mcp_manager, _set_mcp_manager
from twinkle.agentserver.tools.manager import ToolManager


class _FakeClient:
    def __init__(self, name, tools=None, connect_exc=None, call_text="out"):
        self.name = name
        self._tools = tools or []
        self._connect_exc = connect_exc
        self._call_text = call_text
        self.connected = False
    async def connect(self):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
    async def disconnect(self):
        self.connected = False
    async def list_tools(self):
        from twinkle.agentserver.mcp.tool import McpToolCard
        return [McpToolCard(name=f"{self.name}.{t}", server_name=self.name,
                            description=d, parameters=s)
                for t, d, s in self._tools]
    async def call_tool(self, name, arguments, *, timeout=None):
        return self._call_text


def _factory(clients):
    """client factory:按 server.name 返回预构造的 fake client。"""
    by_name = {c.name: c for c in clients}
    def _make(config, connect_timeout, call_timeout, reconnect_attempts=0):
        return by_name[config.name]
    return _make


def _cfg(servers):
    from twinkle.config.schema import McpConfig, McpServerConfig
    return McpConfig(enabled=True, servers=servers)


def test_startup_connects_and_stores_tools() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {"type": "object"})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    assert fake.connected
    assert "fs.read" in [t.card.name for t in mgr._tools.values()]


def test_startup_skips_failed_server_does_not_block() -> None:
    from twinkle.config.schema import McpServerConfig
    srv_ok = McpServerConfig(name="ok", transport="streamable-http", url="http://x")
    srv_bad = McpServerConfig(name="bad", transport="streamable-http", url="http://y")
    ok = _FakeClient("ok", tools=[("ping", "d", {})])
    bad = _FakeClient("bad", connect_exc=ConnectionError("down"))
    mgr = McpManager(_cfg([srv_ok, srv_bad]), client_factory=_factory([ok, bad]))
    asyncio.run(mgr.startup())  # no raise
    assert ok.connected
    assert not bad.connected
    assert "ok.ping" in [t.card.name for t in mgr._tools.values()]
    assert not any("bad." in t.card.name for t in mgr._tools.values())


def test_register_into_injects_tools() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="my", transport="streamable-http", url="http://x")
    fake = _FakeClient("my", tools=[("search", "d", {"type": "object"})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    tm = ToolManager()
    mgr.register_into(tm)
    names = {t.card.name for t in tm.list()}
    assert "my.search" in names


def test_register_into_noop_when_not_started() -> None:
    mgr = McpManager(_cfg([]), client_factory=_factory([]))
    tm = ToolManager()
    mgr.register_into(tm)  # no raise, no tools
    assert tm.list() == []


def test_release_disconnects_all() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="my", transport="streamable-http", url="http://x")
    fake = _FakeClient("my", tools=[("t", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    assert fake.connected
    asyncio.run(mgr.release())
    assert not fake.connected


def test_singleton_and_test_hook() -> None:
    _set_mcp_manager(None)
    a = get_mcp_manager()
    b = get_mcp_manager()
    assert a is b
    fake = McpManager(_cfg([]), client_factory=_factory([]))
    _set_mcp_manager(fake)
    assert get_mcp_manager() is fake
    _set_mcp_manager(None)
