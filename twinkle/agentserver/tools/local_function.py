"""LocalFunction — the local-Python-function implementation of Tool.

Bundles a ToolCard (metadata) with a Callable (execution) and exposes a
single `invoke` entry point. This is one specific tool kind; future MCP
tools would be a sibling implementation of the same Tool interface.
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
