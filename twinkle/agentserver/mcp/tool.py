"""MCP 工具适配 Tool 协议:card + invoke。共享底层 McpClient(不重复连)。"""
from __future__ import annotations

from dataclasses import dataclass

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.errors import ToolError


@dataclass
class McpToolCard(ToolCard):
    """带 server_name 的 ToolCard。name 形如 '{server}.{tool}'。"""
    server_name: str


def extract_text_content(result) -> str:
    """从 MCP CallToolResult.content 取 text。content[-1] 的 text 字段;无 text 返回空串。
    对齐 jiuwenswarm extract_mcp_tool_result_content。"""
    content = getattr(result, "content", None) or []
    if not content:
        return ""
    last = content[-1]
    if getattr(last, "type", None) == "text":
        return getattr(last, "text", "") or ""
    return ""


class McpTool:
    """MCP server 工具,实现 Tool 协议(card + invoke)。共享底层 McpClient。

    card.name = '{server}.{tool}'(进 permissions.tools 按名配 tier + LLM schema);
    invoke 调 client.call_tool 时传裸 tool_name(MCP server 只认自己的工具名)。
    """

    def __init__(self, client: "McpClient", card: McpToolCard) -> None:
        self._client = client
        self._card = card
        # 裸 tool 名:card.name 去掉 '{server_name}.' 前缀
        prefix = card.server_name + "."
        self._tool_name = card.name[len(prefix):] if card.name.startswith(prefix) else card.name

    @property
    def card(self) -> ToolCard:
        return self._card

    async def invoke(self, args: dict) -> str:
        try:
            return await self._client.call_tool(self._tool_name, args)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{self._card.name}: {exc}") from exc
