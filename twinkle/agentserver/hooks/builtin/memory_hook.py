"""MemoryHook — before_model_call injects the long-term-memory usage-strategy prompt.

No-op when the memory store is empty. Injects by assigning a NEW list (never
mutates the store's internal list in place), mirroring SkillHook. 5a ships only
the 'proactive' prompt; a 'passive' variant is a future easy-add (config + a
second prompt string, no code-logic change).
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
        if not get_memory_manager().list_files():
            return  # empty store → no-op
        self._prepend(ctx, _build_prompt())

    @staticmethod
    def _prepend(ctx: HookContext, content: str) -> None:
        # assign a new list — msgs may be the store's internal list
        ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages


def _build_prompt() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(
        mem_dir=MEMORY_DIR, today=datetime.date.today().isoformat())
