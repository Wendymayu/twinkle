"""Foundation layer: ToolCard (pure metadata) + Tool (interface).

Twinkle's four-layer tool model (aligned with openjiuwen
foundation/tool/base.py, cut to a minimal subset):
  ToolCard        — pure description data (name/description/parameters)
  Tool            — the interface any tool kind must satisfy (card + invoke)
  LocalFunction   — local-Python-function implementation of Tool
  ToolManager     — container of Tool, knows only the Tool interface
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolCard:
    name: str
    description: str
    parameters: dict  # OpenAI function-calling `parameters` JSON schema


class Tool(Protocol):
    """Any tool must expose its metadata card and an invoke entry point."""

    card: ToolCard

    async def invoke(self, args: dict) -> str: ...
