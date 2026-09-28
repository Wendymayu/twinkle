"""Memory 工具 —— 模型驱动的长期记忆读/写/搜索/编辑。

get_memory_manager() 的薄封装,对照 skill_tools。全部返回字符串;
错误以返回值给出(绝不抛出),这样一次坏调用不会炸掉 ReAct。
"""
from __future__ import annotations

import re

from twinkle.agentserver.memory import get_memory_manager
from twinkle.agentserver.memory.dreaming import DreamingOrchestrator
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
    """向长期记忆写入事实。path: USER.md(用户画像)、MEMORY.md
    (决策/偏好/持久事实)或 daily_memory/YYYY-MM-DD.md(每日笔记 /
    用户说"记住这个"时)。每条事实写成一个 `- ` bullet(一行一个原子
    事实点,不堆叠多事实/不写段落叙述/不复述对话),这样每条 bullet 独立
    可检索。相对时间(去年/昨天/last year)换算成绝对日期写入,避免记忆
    随时间漂移成错误事实。"""
    mgr = get_memory_manager()
    result = mgr.write(path, content, append=append)
    if path == "MEMORY.md" and not result.startswith("Error"):
        # 写后同步 compact:让 max_memory_chars 存储上限在 agent 写路径生效
        # (dreaming 后台 task 受 busy-backoff/300s 首跑延迟,评测时常没跑→compact 没触发)。
        # 复用 dreaming staticmethod:load sidecar(空则空 state)→ compact(自带 _save_state)。
        sidecar = mgr.memory_dir / "dreaming_state.json"
        state = DreamingOrchestrator._load_state(sidecar)
        DreamingOrchestrator._compact_if_over_budget(mgr, state)
    return result


@tool
async def read_memory(path: str, offset: int | None = None,
                      limit: int | None = None) -> str:
    """读取 memory 文件内容(按行的 offset/limit 分页)。"""
    return get_memory_manager().read(path, offset=offset, limit=limit)
