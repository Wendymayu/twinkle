"""Memory 工具 —— 模型驱动的长期记忆读/写/搜索/编辑。

get_memory_manager() 的薄封装,对照 skill_tools。全部返回字符串;
错误以返回值给出(绝不抛出),这样一次坏调用不会炸掉 ReAct。
"""
from __future__ import annotations

from twinkle.agentserver.memory import get_memory_manager
from twinkle.agentserver.tools.decorator import tool


@tool
async def memory_search(query: str, max_results: int | None = None) -> str:
    """搜索长期记忆中相关的事实。当答案依赖跨 session 的用户偏好、
    历史或过往决策时调用。"""
    hits = get_memory_manager().search(query, max_results=max_results)
    if not hits:
        return "No relevant memories found."
    lines = [f"## 记忆召回 ({len(hits)} 条)"]
    for h in hits:
        lines.append(f"### {h['path']} (score {h['score']})\n{h['text']}")
    return "\n\n".join(lines)


@tool
async def write_memory(path: str, content: str, append: bool = False) -> str:
    """向长期记忆写入一条事实。path: USER.md(用户画像)、MEMORY.md
    (决策/偏好/持久事实)或 daily_memory/YYYY-MM-DD.md(每日笔记 /
    用户说"记住这个"时)。"""
    return get_memory_manager().write(path, content, append=append)


@tool
async def read_memory(path: str, offset: int | None = None,
                      limit: int | None = None) -> str:
    """读取 memory 文件内容(按行的 offset/limit 分页)。"""
    return get_memory_manager().read(path, offset=offset, limit=limit)


@tool
async def edit_memory(path: str, old_text: str, new_text: str) -> str:
    """编辑 memory 文件:把 old_text 的第一处出现替换为 new_text。
    用于修正过时或被推翻的记忆。"""
    return get_memory_manager().edit(path, old_text, new_text)
