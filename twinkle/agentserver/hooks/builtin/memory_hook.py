"""MemoryHook — before_model_call injects the long-term-memory usage-strategy prompt.

No-op when the memory store is empty. Injects by assigning a NEW list (never
mutates the store's internal list in place), mirroring SkillHook. 始终注入策略
prompt（proactive）；opt-in（memory.auto_inject.enabled）时附加被动召回段：把 USER.md +
MEMORY.md + 今日 daily 注入 system prompt，模型不主动 memory_search 也能看到。
"""
from __future__ import annotations

import datetime

from twinkle.agentserver.hooks.base import AgentHook, HookContext

_PROMPT_TEMPLATE = """## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/{today}.md", ...)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""


class MemoryHook(AgentHook):
    priority = 80  # functional layer (50-99); below SkillHook(90)

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        if not mgr.list_files():
            return  # empty store → no-op
        prompt = _build_prompt()
        extra = _build_auto_inject(mgr)
        if extra:
            prompt = prompt + "\n\n" + extra
        self._prepend(ctx, prompt)

    @staticmethod
    def _prepend(ctx: HookContext, content: str) -> None:
        # assign a new list — msgs may be the store's internal list
        ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages


def _build_prompt() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(
        mem_dir=MEMORY_DIR, today=datetime.date.today().isoformat())


def _build_auto_inject(mgr) -> str:
    """被动召回：opt-in 时把 USER.md + MEMORY.md + 今日 daily 注入 system prompt。

    模型不主动 memory_search 也能看到长期记忆。开关关或无可注入文件 → 返回空串
    （before_model_call 只注入策略 prompt）。超 max_chars 截断并提示用 memory_search。
    """
    from twinkle.config import MEMORY_AUTO_INJECT_ENABLED, MEMORY_AUTO_INJECT_MAX_CHARS
    if not MEMORY_AUTO_INJECT_ENABLED:
        return ""
    today = datetime.date.today().isoformat()
    sections: list[str] = []
    user_md = mgr.read("USER.md")
    if not user_md.startswith("Error:"):
        sections.append(f"### 用户画像（USER.md）\n{user_md}")
    mem_md = mgr.read("MEMORY.md")
    if not mem_md.startswith("Error:"):
        sections.append(f"### 持久事实（MEMORY.md）\n{mem_md}")
    daily = mgr.read(f"daily_memory/{today}.md")
    if not daily.startswith("Error:"):
        sections.append(f"### 今日记录（daily_memory/{today}.md）\n{daily}")
    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > MEMORY_AUTO_INJECT_MAX_CHARS:
        body = body[:MEMORY_AUTO_INJECT_MAX_CHARS] + "\n…[被动召回注入已截断,更多用 memory_search 查]"
    return "## 被动召回（自动注入的长期记忆）\n" + body
