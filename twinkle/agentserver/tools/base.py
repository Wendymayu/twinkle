"""基础层:ToolCard(纯元数据)+ Tool(接口)。

Twinkle 的四层 tool 模型(对齐 openjiuwen foundation/tool/base.py,
裁剪到最小子集):
  ToolCard        — 纯描述数据(name/description/parameters)
  Tool            — 任意 tool 类型都须满足的接口(card + invoke)
  LocalFunction   — Tool 的本地 Python 函数实现
  ToolManager     — Tool 的容器,只知 Tool 接口
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ToolCard:
    name: str
    description: str
    parameters: dict  # OpenAI function-calling 的 `parameters` JSON schema


@runtime_checkable
class Tool(Protocol):
    """任意 tool 都须暴露其元数据 card 和一个 invoke 入口。

    ``@runtime_checkable`` 让 ``isinstance(t, Tool)`` 可校验结构一致性
    (是否存在 ``card`` + ``invoke``)—— tool 测试用到。
    """

    card: ToolCard

    async def invoke(self, args: dict) -> str: ...
