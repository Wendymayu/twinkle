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
from typing import Protocol, runtime_checkable


@dataclass
class ToolCard:
    name: str
    description: str
    parameters: dict  # OpenAI function-calling `parameters` JSON schema


@runtime_checkable
class Tool(Protocol):
    """Any tool must expose its metadata card and an invoke entry point.

    ``@runtime_checkable`` lets ``isinstance(t, Tool)`` verify structural
    conformance (presence of ``card`` + ``invoke``) — used by tool tests.
    """

    card: ToolCard

    async def invoke(self, args: dict) -> str: ...
