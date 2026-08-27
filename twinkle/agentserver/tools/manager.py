"""ToolManager —— Tool 的容器。只知 Tool 接口。

对齐 openjiuwen core/single_agent/ability_manager.py,裁剪到最小子集:
register/unregister/list/get/schemas/execute。无 catalog()(YAGNI ——
list() 已覆盖枚举,schemas() 已覆盖模型视图)。
"""
from __future__ import annotations

from twinkle.agentserver.tools.base import Tool
from twinkle.agentserver.tools.errors import ToolError


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
            raise ToolError(f"unknown tool: {name}", kind="validation")
        # Tool 抛出的异常在此传播(不被吞掉),以便 @hook 装饰的
        # _hooked_tool_call 能触发 ON_TOOL_EXCEPTION 供观测
        # (AuditHook / RepeatToolCallDetectorHook)。工具层不再重试
        # (2026-08-27 移除:瞬时网络异常重试会重复执行有副作用的方法体,
        # 无幂等保护)。agent loop 把失败转成 "[tool error] ..." tool_result
        # 字符串 —— loop 仍不会崩溃。
        return await t.invoke(args)
