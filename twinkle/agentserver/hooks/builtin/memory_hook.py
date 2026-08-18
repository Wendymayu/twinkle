"""MemoryHook — before_invoke 注 strategy + opt-in 静态召回(USER.md/MEMORY.md)到 ctx.extra["frozen_sections"]。

No-op when the memory store is empty. 注入走 ctx.extra["frozen_sections"](loop 每步套用到 builder):
- memory_strategy(priority 80):何时搜/写的策略 prompt(稳定,常开;提示需 daily 时 memory_search)。
- memory_static(priority 81, opt-in):USER.md + MEMORY.md 各按自己字符预算注入(超限 head+tail 截断,对齐 openclaw)。
daily 不再自动注入——需 daily 时 memory_search('daily_memory/<日期>')(= tool message = 动态区)。
"""
from __future__ import annotations

from twinkle.agentserver.hooks.base import AgentHook, HookContext
from twinkle.agentserver.prompts import PromptSection

_PROMPT_TEMPLATE = """## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。
需要今日/昨日记录时,先 memory_search('daily_memory/<日期>')(今日日期见下方环境信息)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/<今日日期>.md", ...)(今日日期见下方环境信息)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""


class MemoryHook(AgentHook):
    priority = 80  # functional layer (50-99); below SkillHook(90)

    async def before_invoke(self, ctx: HookContext) -> None:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        if not mgr.list_files():
            return  # empty store → no-op
        frozen = ctx.extra.setdefault("frozen_sections", [])
        frozen.append(PromptSection("memory_strategy", _build_prompt(), priority=80))
        static = _build_static(mgr)
        if static:
            frozen.append(PromptSection("memory_static", static, priority=81))


def _build_prompt() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(mem_dir=MEMORY_DIR)


_TRUNCATE_MARKER = "\n…[已截断,首尾保留,更多用 memory_search 查]\n"


def _truncate_head_tail(text: str, max_chars: int) -> str:
    """超 max_chars → 保首尾丢中间(对齐 openclaw trimBootstrapContent)。

    首部=画像/核心偏好(稳定),尾部=最近事实(新);丢中间陈旧段。budget 留给 marker 后首尾各半。
    """
    if len(text) <= max_chars:
        return text
    budget = max(0, max_chars - len(_TRUNCATE_MARKER))
    head = budget // 2
    tail = budget - head
    return text[:head] + _TRUNCATE_MARKER + text[-tail:]


def _build_static(mgr) -> str:
    """opt-in 时把 USER.md + MEMORY.md 注入(读一次/invoke,无 daily)。

    USER.md 与 MEMORY.md 各走自己的字符预算(对齐 openclaw 分文件预算),超限各自 head+tail 截断。
    开关关或无可注入文件 → 返回空串(只注策略)。
    daily 不再自动注入——需要时模型 memory_search('daily_memory/<日期>')。
    """
    from twinkle.config import (
        MEMORY_AUTO_INJECT_ENABLED,
        MEMORY_AUTO_INJECT_MAX_CHARS_USER,
        MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY,
    )
    if not MEMORY_AUTO_INJECT_ENABLED:
        return ""
    sections: list[str] = []
    user_md = mgr.read("USER.md")
    if not user_md.startswith("Error:"):
        user_md = _truncate_head_tail(user_md, MEMORY_AUTO_INJECT_MAX_CHARS_USER)
        sections.append(f"### 用户画像（USER.md）\n{user_md}")
    mem_md = mgr.read("MEMORY.md")
    if not mem_md.startswith("Error:"):
        mem_md = _truncate_head_tail(mem_md, MEMORY_AUTO_INJECT_MAX_CHARS_MEMORY)
        sections.append(f"### 持久事实（MEMORY.md）\n{mem_md}")
    if not sections:
        return ""
    return "## 被动召回（自动注入的长期记忆）\n" + "\n\n".join(sections)
