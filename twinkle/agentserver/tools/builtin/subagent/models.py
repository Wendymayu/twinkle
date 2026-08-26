"""Subagent 数据模型 + 排除工具集 + 软超时标记。

EXCLUDED_TOOLS 是递归 + 能力守卫:子的 ToolManager 从父的工具中
减去此集合。spawn_subagent => 无递归(单层);write_memory/edit_memory
=> 子的 memory 只读。
"""
from __future__ import annotations

from pydantic import BaseModel


class SoftTimeoutError(Exception):
    """子 agent 流式活动静默已 soft_timeout 秒。"""


EXCLUDED_TOOLS: set[str] = {
    "spawn_subagent",          # 递归守卫:子不能再委派
    "write_memory",            # 子的 memory 只读
    "edit_memory",
}


class SubagentTaskSpec(BaseModel):
    objective: str
    prompt: str = ""


class SubagentResult(BaseModel):
    success: bool
    result: str | None = None
    error: str | None = None
