# twinkle/agentserver/mcp/manager.py
"""McpManager — 进程级单例(对齐 get_memory_manager)。eager 连 + 拉工具 + 注入 ToolManager + release。"""
from __future__ import annotations

import asyncio
import logging
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


class McpManager:
    def __init__(self, config, client_factory: Callable[..., McpClient] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._clients: list[McpClient] = []
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
                for card in cards:
                    tool = McpTool(client=client, card=card)
                    self._tools[tool.card.name] = tool
                    log.info("mcp tool registered: %s", tool.card.name)

    def register_into(self, tm: ToolManager) -> None:
        for tool in self._tools.values():
            tm.register(tool)

    async def release(self) -> None:
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception as exc:
                log.warning("mcp client %s disconnect error: %s", client.name, exc)
        self._clients.clear()
        self._tools.clear()

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


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
