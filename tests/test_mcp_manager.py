# tests/test_mcp_manager.py
import asyncio
import time
import pytest
from twinkle.agentserver.mcp.manager import McpManager, get_mcp_manager, _set_mcp_manager
from twinkle.agentserver.tools.manager import ToolManager


class _FakeClient:
    def __init__(self, name, tools=None, connect_exc=None, call_text="out",
                 list_tools_exc=None):
        self.name = name
        self._tools = tools or []
        self._connect_exc = connect_exc
        self._call_text = call_text
        self.list_tools_exc = list_tools_exc
        self.list_tools_call_count = 0
        self.connected = False
    async def connect(self):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
    async def disconnect(self):
        self.connected = False
    async def list_tools(self):
        self.list_tools_call_count += 1
        if self.list_tools_exc:
            raise self.list_tools_exc
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
    asyncio.run(mgr.startup())  # 不抛
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
    mgr.register_into(tm)  # 不抛,无工具
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
    assert mgr._server_resources == {}


def test_singleton_and_test_hook() -> None:
    _set_mcp_manager(None)
    a = get_mcp_manager()
    b = get_mcp_manager()
    assert a is b
    fake = McpManager(_cfg([]), client_factory=_factory([]))
    _set_mcp_manager(fake)
    assert get_mcp_manager() is fake
    _set_mcp_manager(None)


def test_startup_populates_server_resources() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {"type": "object"}), ("write", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    res = mgr._server_resources["fs"]
    assert res.name == "fs"
    assert res.client is fake
    assert res.tool_names == {"fs.read", "fs.write"}
    assert res.expiry == 300.0          # McpConfig 默认
    assert isinstance(res.last_update, float)


def test_refresh_all_within_ttl_no_fetch() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 把 last_update 设成"刚刷过",TTL(300) 内
    mgr._server_resources["fs"].last_update = time.time()
    diffs = asyncio.run(mgr.refresh_all())
    assert diffs == []
    assert fake.list_tools_call_count == 1   # 仅 startup 调过,refresh 没调


def test_refresh_all_expired_fetches_and_diffs() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 模拟 server 端工具变了:read 删了,write 新增
    fake._tools = [("write", "d", {})]
    # 让 TTL 过期
    mgr._server_resources["fs"].last_update = time.time() - 301
    diffs = asyncio.run(mgr.refresh_all())
    assert len(diffs) == 1
    assert diffs[0].removed == ["fs.read"]
    assert [t.card.name for t in diffs[0].added] == ["fs.write"]
    assert "fs.write" in mgr._tools
    assert "fs.read" not in mgr._tools
    assert mgr._server_resources["fs"].tool_names == {"fs.write"}


def test_refresh_all_failure_degrades_keep_old() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # 让 list_tools 抛异常 + TTL 过期
    fake.list_tools_exc = RuntimeError("server down")
    mgr._server_resources["fs"].last_update = time.time() - 301
    diffs = asyncio.run(mgr.refresh_all())
    assert diffs == []                       # 该 server 无 diff
    assert "fs.read" in mgr._tools           # 旧清单保留
    assert mgr._server_resources["fs"].tool_names == {"fs.read"}  # 未更新


def test_refresh_all_force_bypasses_ttl() -> None:
    from twinkle.config.schema import McpServerConfig
    srv = McpServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "p"])
    fake = _FakeClient("fs", tools=[("read", "d", {})])
    mgr = McpManager(_cfg([srv]), client_factory=_factory([fake]))
    asyncio.run(mgr.startup())
    # TTL 内(刚刷过),但 force=True 应绕过 TTL 真拉
    mgr._server_resources["fs"].last_update = time.time()
    fake._tools = [("write", "d", {})]  # 工具变了,验证真拉+diff
    diffs = asyncio.run(mgr.refresh_all(force=True))
    assert len(diffs) == 1
    assert diffs[0].removed == ["fs.read"]
    assert [t.card.name for t in diffs[0].added] == ["fs.write"]
    assert fake.list_tools_call_count == 2  # startup 1 + force 刷新 1
