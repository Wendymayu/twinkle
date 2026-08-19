"""MCP 接入包 — 进程级单例 + 测试钩子。"""
from twinkle.agentserver.mcp.manager import (
    McpManager, get_mcp_manager, _set_mcp_manager,
)

__all__ = ["McpManager", "get_mcp_manager", "_set_mcp_manager"]
