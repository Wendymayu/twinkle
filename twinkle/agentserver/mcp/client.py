"""MCP 传输客户端。StdioMcpClient(本地子进程)+ StreamableHttpMcpClient(远端/本机 HTTP)。
用官方 mcp SDK;SDK 交互收口到 _open_session(production 调 SDK,测试 override)。"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack

from twinkle.agentserver.mcp.tool import McpToolCard, extract_text_content
from twinkle.agentserver.tools.errors import ToolError


class McpClient(ABC):
    __client_name__: str = ""

    def __init__(self, config, connect_timeout: float, call_timeout: float,
                 reconnect_attempts: int = 0) -> None:
        self._config = config
        self._name = config.name
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._reconnect_attempts = reconnect_attempts
        self._stack: AsyncExitStack | None = None
        self._session = None

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    async def _open_session(self, stack: AsyncExitStack):
        """production: 调 mcp SDK 建 ClientSession 并 initialize;测试 override 返回 fake。"""

    async def connect(self) -> None:
        self._stack = AsyncExitStack()
        try:
            self._session = await asyncio.wait_for(
                self._open_session(self._stack), timeout=self._connect_timeout)
        except Exception:
            await self._cleanup()
            raise

    async def _cleanup(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._session = None

    async def disconnect(self) -> None:
        await self._cleanup()

    async def list_tools(self) -> list[McpToolCard]:
        resp = await self._session.list_tools()
        return [
            McpToolCard(name=f"{self._name}.{t.name}", server_name=self._name,
                         description=getattr(t, "description", "") or "",
                         parameters=getattr(t, "inputSchema", {}) or {})
            for t in resp.tools
        ]

    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str:
        to = timeout or self._config.timeout or self._call_timeout
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments), timeout=to)
        except Exception as exc:
            raise ToolError(f"{self._name}.{name}: {exc}") from exc
        return extract_text_content(result)


class StdioMcpClient(McpClient):
    __client_name__ = "stdio"

    async def _open_session(self, stack: AsyncExitStack):
        # production: 调官方 mcp SDK。对齐 jiuwenswarm(mcp 1.29 API)。
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from twinkle.agentserver.mcp.safety import check_dangerous_args
        check_dangerous_args(self._config.args)
        params = StdioServerParameters(
            command=self._config.command, args=self._config.args, env=self._config.env or None)
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session


class StreamableHttpMcpClient(McpClient):
    __client_name__ = "streamable-http"

    async def _open_session(self, stack: AsyncExitStack):
        # production: 调官方 mcp SDK。mcp>=1.26 的 streamable_http_client yield (read, write, get_session_id)。
        # 若所装版本 yield 2 元组,改成 `async with streamable_http_client(url, ...) as (read, write):`。
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        read, write, _get_session_id = await stack.enter_async_context(
            streamable_http_client(
                self._config.url,
                headers=self._config.auth_headers or None,
                timeout=self._call_timeout,
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def list_tools(self) -> list[McpToolCard]:
        return await self._retry(super().list_tools)

    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str:
        bound = super().call_tool
        return await self._retry(lambda: bound(name, arguments, timeout=timeout))

    async def _retry(self, fn):
        """挂 with_reconnect:可重试传输错误 → disconnect+connect 重试。"""
        from twinkle.agentserver.mcp.reconnect import with_reconnect

        @with_reconnect
        async def _do(client):
            return await fn()

        return await _do(self, attempts=self._reconnect_attempts)
