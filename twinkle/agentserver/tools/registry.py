"""Minimal tool registry — static registration, OpenAI function-calling schema.

Phase 2 evolves this into dynamic registration + a catalog. Phase 1 keeps
just enough to expose two read-only web tools to the agent loop.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

ToolFn = Callable[..., Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: ToolFn,
    ) -> None:
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "execute": execute,
        }

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for name, t in self._tools.items()
        ]

    async def execute(self, name: str, args: dict) -> str:
        t = self._tools.get(name)
        if t is None:
            return f"[error] unknown tool: {name}"
        try:
            return await t["execute"](**args)
        except Exception as exc:  # tool failures must not crash the loop
            return f"[tool error] {type(exc).__name__}: {exc}"
