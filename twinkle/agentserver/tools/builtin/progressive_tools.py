"""Progressive tool visibility meta-tools: tools_search + invoke_tool.

手写 Tool 实现(非 @tool,需持有 ToolManager 引用)。对齐 jiuwenswarm
deep_agent/rails/jiuwen_progressive_tool_rail.py 的两个 meta-tool,但简化:
deferred 工具实例统一在 ToolManager,invoke_tool 直接转调 tm.execute,
省 jiuwenswarm 的 resource_mgr 间接层。

invoke 返回 JSON 字符串(对齐 Twinkle Tool.invoke -> str 协议)。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from twinkle.agentserver.tools.base import ToolCard

META_NAMES = frozenset({"tools_search", "invoke_tool"})

_TOOLS_SEARCH_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {
            "type": "string",
            "description": "按需可见工具的注册名称(与系统导航列表中的名称一致,精确匹配,忽略大小写)",
        }
    },
    "required": ["tool_name"],
}

_INVOKE_TOOL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string", "description": "按需可见工具的注册名称"},
        "arguments": {
            "type": "object",
            "description": "根据 tools_search 返回的 input_schema 构造的参数",
        },
    },
    "required": ["tool_name", "arguments"],
}


def deferred_schemas(tm: Any, eager_names: Iterable[str]) -> list[dict]:
    """返回 deferred 工具的 schema 列表(tm 全量 - eager - meta)。"""
    eager = set(eager_names) | META_NAMES
    return [s for s in tm.schemas() if s["function"]["name"] not in eager]


class ToolsSearchTool:
    """tools_search: 按 deferred 工具注册名精确(大小写不敏感)查完整 schema。"""

    def __init__(self, tm: Any, eager_names: Iterable[str]) -> None:
        self._tm = tm
        # 存引用(builder 传同一 eager set 给 hook + 两 meta-tool):hook.before_invoke 重算
        # 改共享 set,本 tool 的 deferred 判断随之同步。
        self._eager = eager_names if isinstance(eager_names, set) else set(eager_names)
        self.card = ToolCard(
            name="tools_search",
            description=(
                "按工具注册名查询按需可见(deferred)工具的完整 input_schema。"
                "入参 tool_name 须与系统导航列表中的名称一致(精确匹配,忽略大小写)。"
                "调用 invoke_tool 前必须先调本工具获取 schema,再据此构造 arguments。"
            ),
            parameters=_TOOLS_SEARCH_PARAMS,
        )

    async def invoke(self, args: dict) -> str:
        tool_name = str(args.get("tool_name", "")).strip()
        key = tool_name.lower()
        if not key:
            return json.dumps(
                {"success": False, "matches": [], "message": "tool_name is required"},
                ensure_ascii=False,
            )
        matches = []
        for s in deferred_schemas(self._tm, self._eager):
            name = s["function"]["name"]
            if name.lower() == key:
                matches.append({
                    "name": name,
                    "description": s["function"]["description"],
                    "input_schema": s["function"]["parameters"],
                })
        if matches:
            message = (
                f"已找到工具 '{tool_name}',请根据 input_schema 构造 arguments "
                f"后调用 invoke_tool。"
            )
        else:
            message = (
                f"未找到名为 '{tool_name}' 的按需可见工具,"
                f"请检查名称是否与导航列表一致。"
            )
        return json.dumps(
            {"success": bool(matches), "matches": matches,
             "count": len(matches), "message": message},
            ensure_ascii=False,
        )


class InvokeToolTool:
    """invoke_tool: 按名间接调用 deferred 工具(转调 ToolManager.execute)。"""

    def __init__(self, tm: Any, eager_names: Iterable[str]) -> None:
        self._tm = tm
        # 存引用(builder 传同一 eager set 给 hook + 两 meta-tool):hook.before_invoke 重算
        # 改共享 set,本 tool 的 deferred 判断随之同步。
        self._eager = eager_names if isinstance(eager_names, set) else set(eager_names)
        self.card = ToolCard(
            name="invoke_tool",
            description=(
                "按名间接调用按需可见(deferred)工具。先 tools_search 拿 schema,"
                "再据此构造 arguments。"
            ),
            parameters=_INVOKE_TOOL_PARAMS,
        )

    async def invoke(self, args: dict) -> str:
        tool_name = str(args.get("tool_name", "")).strip()
        arguments = args.get("arguments", {}) or {}
        deferred_names = {
            s["function"]["name"] for s in deferred_schemas(self._tm, self._eager)
        }
        if tool_name not in deferred_names:
            # 预期错误:eager 工具或未知名 → 返回引导 JSON(条件 return,无需 try)
            return json.dumps(
                {"success": False,
                 "error": f"'{tool_name}' 不是按需可见工具"
                          f"(可能已在 tools 列表或不存在),请先 tools_search 确认。"},
                ensure_ascii=False,
            )
        # 被调工具的异常让其传播到 agent loop 的 format_tool_error(对齐直接调用)
        result = await self._tm.execute(tool_name, arguments)
        return result  # 被调工具本就返回 str(对齐 Tool.invoke 协议)
