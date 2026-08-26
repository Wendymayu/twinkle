"""LocalFunction —— Tool 的本地 Python 函数实现。

把 ToolCard(元数据)与 Callable(执行体)打包在一起,暴露唯一的 `invoke`
入口。这是一种具体的 tool 类型;未来的 MCP 工具将是同一 Tool 接口的
并列实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from twinkle.agentserver.tools.base import ToolCard


@dataclass
class LocalFunction:
    card: ToolCard
    func: Callable[..., Awaitable[str]]

    async def invoke(self, args: dict) -> str:
        return await self.func(**args)
