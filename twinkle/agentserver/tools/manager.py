"""ToolManager — container of Tool. Knows only the Tool interface.

Aligned with openjiuwen core/single_agent/ability_manager.py, cut to a
minimal subset: register/unregister/list/get/schemas/execute. No catalog()
(YAGNI — list() covers enumeration, schemas() covers the model view).
"""
from __future__ import annotations

from twinkle.agentserver.tools.base import Tool


class ToolManager:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.card.name] = tool

    def unregister(self, name: str) -> bool:
        existed = name in self._tools
        if existed:
            del self._tools[name]
        return existed

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.card.name,
                    "description": t.card.description,
                    "parameters": t.card.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, args: dict) -> str:
        t = self._tools.get(name)
        if t is None:
            return f"[error] unknown tool: {name}"
        try:
            return await t.invoke(args)
        except Exception as exc:  # tool failures must not crash the loop
            return f"[tool error] {type(exc).__name__}: {exc}"
