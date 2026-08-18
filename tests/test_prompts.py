# tests/test_prompts.py
"""SystemPromptBuilder — dict-by-name section 覆写(同名不堆叠) + priority 排序 join。"""
from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder


def test_add_section_overwrites_same_name_not_stack():
    """同名 section 后者覆写前者(不堆叠)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("skills", "v1", priority=90))
    b.add_section(PromptSection("skills", "v2", priority=90))
    assert b.build() == "v2"


def test_build_sorts_by_priority():
    """build() 按 priority 升序 join(小在前)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("memory", "MEM", priority=80))
    b.add_section(PromptSection("skills", "SKILL", priority=90))
    b.add_section(PromptSection("identity", "ID", priority=10))
    assert b.build() == "ID\n\nMEM\n\nSKILL"


def test_remove_section():
    b = SystemPromptBuilder()
    b.add_section(PromptSection("skills", "S", priority=90))
    b.remove_section("skills")
    assert b.build() == ""
    # remove 不存在的 name 不报错
    b.remove_section("nope")


def test_build_empty_returns_empty_string():
    assert SystemPromptBuilder().build() == ""


def test_build_is_idempotent_and_deterministic():
    """稳定 section 内容不变 → build() 输出不变(prefix cache 友好的前提)。"""
    b = SystemPromptBuilder()
    b.add_section(PromptSection("identity", "STABLE", priority=10))
    b.add_section(PromptSection("skills", "ALSO_STABLE", priority=90))
    assert b.build() == b.build()
