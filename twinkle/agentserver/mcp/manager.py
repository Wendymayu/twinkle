# twinkle/agentserver/mcp/manager.py
"""McpManager — 进程级单例(对齐 get_memory_manager)。eager 连 + 拉工具 + 注入 ToolManager + release。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from twinkle.agentserver.mcp.client import McpClient, StdioMcpClient, StreamableHttpMcpClient
from twinkle.agentserver.mcp.tool import McpTool
from twinkle.agentserver.tools.base import Tool
from twinkle.agentserver.tools.manager import ToolManager

log = logging.getLogger("twinkle.mcp")


def _default_client_factory(config, connect_timeout, call_timeout, reconnect_attempts=0) -> McpClient:
    if config.transport == "stdio":
        return StdioMcpClient(config, connect_timeout, call_timeout)
    return StreamableHttpMcpClient(config, connect_timeout, call_timeout, reconnect_attempts)


@dataclass
class _McpServerResource:
    name: str
    client: McpClient
    tool_names: set[str]
    last_update: float
    expiry: float | None


@dataclass
class _ToolDiff:
    added: list[Tool]
    removed: list[str]


class McpManager:
    def __init__(self, config, client_factory: Callable[..., McpClient] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clients: list[McpClient] = []
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._server_resources: dict[str, _McpServerResource] = {}

    async def startup(self) -> None:
        for srv in self._config.servers:
            lock = self._locks.setdefault(srv.name, asyncio.Lock())
            async with lock:
                try:
                    client = self._client_factory(
                        srv, self._config.connect_timeout, self._config.call_timeout,
                        self._config.reconnect_attempts)
                    await client.connect()
                    cards = await client.list_tools()
                except Exception as exc:
                    log.warning("mcp server %s connect failed: %s, skipping", srv.name, exc)
                    continue
                self._clients.append(client)
                tool_names: set[str] = set()
                for card in cards:
                    tool = McpTool(client=client, card=card)
                    self._tools[tool.card.name] = tool
                    tool_names.add(tool.card.name)
                    log.info("mcp tool registered: %s", tool.card.name)
                self._server_resources[srv.name] = _McpServerResource(
                    name=srv.name, client=client, tool_names=tool_names,
                    last_update=time.time(), expiry=self._config.tool_refresh_ttl,
                )

    def register_into(self, tm: ToolManager) -> None:
        for tool in self._tools.values():
            tm.register(tool)

    async def refresh_all(self, force: bool = False) -> list[_ToolDiff]:
        """请求边界 TTL 刷新:per-server 过期/force 才重拉 list_tools,diff 后返回。
        不持 tm(方案B 解耦):调用方应用 diff。失败降级(沿用旧清单,返回空)。"""
        if not self._server_resources:
            return []
        diffs: list[_ToolDiff] = []
        now = time.time()
        for res in self._server_resources.values():
            async with self._locks.setdefault(res.name, asyncio.Lock()):
                need = force or (res.expiry is not None and now - res.last_update >= res.expiry)
                if not need:
                    continue
                try:
                    cards = await asyncio.wait_for(
                        res.client.list_tools(), self._config.call_timeout)
                except Exception as exc:
                    log.warning("mcp refresh %s failed, keep old tools: %s", res.name, exc)
                    continue
                new_names = {c.name for c in cards}
                removed = list(res.tool_names - new_names)
                added: list[Tool] = []
                for card in cards:
                    tool = McpTool(client=res.client, card=card)
                    self._tools[card.name] = tool
                    added.append(tool)
                for name in removed:
                    self._tools.pop(name, None)
                res.tool_names = new_names
                res.last_update = time.time()
                diffs.append(_ToolDiff(added=added, removed=removed))
        return diffs

    async def release(self) -> None:
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception as exc:
                log.warning("mcp client %s disconnect error: %s", client.name, exc)
        self._clients.clear()
        self._tools.clear()
        self._server_resources.clear()


_MCP_MANAGER: McpManager | None = None


def get_mcp_manager(config=None) -> McpManager:
    """进程单例(lazy 构造,对齐 get_memory_manager)。config=None 从 settings.mcp 读。
    构造时不连——startup() 才 eager 连。"""
    global _MCP_MANAGER
    if _MCP_MANAGER is None:
        if config is None:
            from twinkle.config import settings
            config = settings.mcp
        _MCP_MANAGER = McpManager(config)
    return _MCP_MANAGER


def _set_mcp_manager(mgr: McpManager | None) -> None:
    """Test hook."""
    global _MCP_MANAGER
    _MCP_MANAGER = mgr
