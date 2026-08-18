# twinkle/agentserver/prompts.py
"""SystemPromptBuilder — dict-by-name section 覆写 + priority 排序 join。

对齐 jiuwenswarm core/single_agent/prompts/builder.py 核心(砍多语言)：
- add_section = _sections[name] = section（同名覆写,不堆叠）
- build() = 按 priority 升序 "\n\n".join(content)

每步 per-request 新建实例。build() 每次全量重建、幂等、确定性。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PromptSection:
    name: str
    content: str
    priority: int


class SystemPromptBuilder:
    """dict-by-name section + priority 排序 + 同名覆写(不堆叠)。"""

    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def add_section(self, section: PromptSection) -> None:
        self._sections[section.name] = section  # 同名覆写

    def remove_section(self, name: str) -> None:
        self._sections.pop(name, None)

    def build(self) -> str:
        return "\n\n".join(
            s.content for s in
            sorted(self._sections.values(), key=lambda x: x.priority)
        )
