"""Memory 工具 —— 模型驱动的长期记忆读/写/搜索/编辑。

get_memory_manager() 的薄封装,对照 skill_tools。全部返回字符串;
错误以返回值给出(绝不抛出),这样一次坏调用不会炸掉 ReAct。
"""
from __future__ import annotations

import re

from twinkle.agentserver.memory import get_memory_manager
from twinkle.agentserver.tools.decorator import tool

_CJK_RE = re.compile(r"[一-鿿]")  # U+4E00–U+9FFF CJK Unified Ideographs(语言检测);fts.py:_CJK_PAT 更宽含 ExtA/兼容字


def _recall_header(query: str, n: int) -> str:
    """返回头跟随 query 语言:CJK→中文,否则英文。避免英文数据评测时中文头
    混入 context 诱导中文作答(治 #73)。"""
    if _CJK_RE.search(query):
        return f"## 记忆召回 ({n} 条)"
    return f"## Memory Recall ({n} hit{'s' if n != 1 else ''})"


@tool
async def memory_search(query: str, max_results: int | None = None) -> str:
    """搜索长期记忆中相关的事实。当答案依赖跨 session 的用户偏好、
    历史或过往决策时调用。"""
    hits = get_memory_manager().search(query, max_results=max_results)
    if not hits:
        return "No relevant memories found."
    lines = [_recall_header(query, len(hits))]
    for h in hits:
        anchor = h.get("section_title", "")
        anchor_tag = f" [§ {anchor}]" if anchor else ""
        lines.append(
            f"### {h['path']} L{h['start_line']}-{h['end_line']}{anchor_tag} "
            f"(score {h['score']})\n{h['text']}")
    return "\n\n".join(lines)


@tool
async def write_memory(path: str, content: str, append: bool = False) -> str:
    """向长期记忆写入一条事实。path: USER.md(用户画像)、MEMORY.md
    (决策/偏好/持久事实)或 daily_memory/YYYY-MM-DD.md(每日笔记 /
    用户说"记住这个"时)。相对时间(去年/昨天/last year)换算成绝对日期
    写入,避免记忆随时间漂移成错误事实。"""
    return get_memory_manager().write(path, content, append=append)


@tool
async def read_memory(path: str, offset: int | None = None,
                      limit: int | None = None) -> str:
    """读取 memory 文件内容(按行的 offset/limit 分页)。"""
    return get_memory_manager().read(path, offset=offset, limit=limit)
