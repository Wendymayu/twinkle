"""@tool 装饰器 —— 把普通 async 函数转成 LocalFunction。

用法:
  @tool                       # 裸装饰器
  @tool()                     # 带括号调用,无参数
  @tool(name=..., input_params=...)   # 覆盖默认值
  tool(fn)                    # 非装饰器形式(tool_manager 使用)
"""
from __future__ import annotations

from typing import Callable, Optional

from twinkle.agentserver.tools.base import ToolCard
from twinkle.agentserver.tools.local_function import LocalFunction
from twinkle.agentserver.tools.schema_extractor import extract


def tool(
    func: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    input_params: Optional[dict] = None,
) -> LocalFunction:
    def _build(f: Callable) -> LocalFunction:
        ex_name, ex_desc, ex_params = extract(f)
        card = ToolCard(
            name=name if name is not None else ex_name,
            description=description if description is not None else ex_desc,
            parameters=input_params if input_params is not None else ex_params,
        )
        return LocalFunction(card=card, func=f)

    if func is not None:
        return _build(func)
    return _build  # type: ignore[return-value]
