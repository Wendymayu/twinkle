"""Memory tools — model-driven read/write/search/edit over long-term memory.

Thin wrappers around get_memory_manager(), mirroring skill_tools. All return
strings; errors are returned (never raised) so a bad call doesn't crash ReAct.
"""
from __future__ import annotations

from twinkle.agentserver.memory import get_memory_manager
from twinkle.agentserver.tools.decorator import tool


@tool
async def memory_search(query: str, max_results: int | None = None) -> str:
    """Search long-term memory for relevant facts. Call when the answer depends on
    cross-session user preferences, history, or past decisions."""
    hits = get_memory_manager().search(query, max_results=max_results)
    if not hits:
        return "No relevant memories found."
    lines = [f"## 记忆召回 ({len(hits)} 条)"]
    for h in hits:
        lines.append(f"### {h['path']} (score {h['score']})\n{h['text']}")
    return "\n\n".join(lines)


@tool
async def write_memory(path: str, content: str, append: bool = False) -> str:
    """Write a fact to long-term memory. path: USER.md (user profile), MEMORY.md
    (decisions/preferences/persistent facts), or daily_memory/YYYY-MM-DD.md (daily
    notes / when the user says 'remember this')."""
    return get_memory_manager().write(path, content, append=append)


@tool
async def read_memory(path: str, offset: int | None = None,
                      limit: int | None = None) -> str:
    """Read a memory file's contents (line-based offset/limit paging)."""
    return get_memory_manager().read(path, offset=offset, limit=limit)


@tool
async def edit_memory(path: str, old_text: str, new_text: str) -> str:
    """Edit a memory file by replacing the first occurrence of old_text with
    new_text. Use to correct stale or contradicted memories."""
    return get_memory_manager().edit(path, old_text, new_text)
