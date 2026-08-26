"""Skill 工具 — 模型用 list_skill 看清单、read_skill 载入 SKILL.md 指令体。

read_skill 把 SKILL.md 正文作 tool_result 回灌(标准 {role:tool}),不抛、不炸 ReAct
(错误走字符串,对齐 todo_tools 的 TodoError 模式)。复用普通文件读(skill 是本地文件)。
"""
from __future__ import annotations

from pathlib import Path

from twinkle.agentserver.skills import get_skill_manager
from twinkle.agentserver.tools.decorator import tool


@tool
async def list_skill() -> str:
    """列出可用 skill(name + description)。调用 read_skill 前先调本工具查看 skill 清单。"""
    skills = get_skill_manager().list_skills()
    if not skills:
        return "No skills available."
    lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
    return "\n".join(lines)


@tool
async def read_skill(skill_name: str, relative_file_path: str = "SKILL.md") -> str:
    """载入一个 skill 的指令。传入从 list_skill 获得的 skill_name;默认读取 SKILL.md。"""
    skill = get_skill_manager().get_skill(skill_name)
    if skill is None:
        return f"Skill '{skill_name}' not found. Call list_skill to see available skills."
    skill_dir = Path(skill.directory).resolve()
    try:
        resolved = (skill_dir / relative_file_path).resolve()
    except OSError:
        return f"Error: cannot resolve path '{relative_file_path}' for skill '{skill_name}'."
    if not resolved.is_relative_to(skill_dir):
        return f"Error: path '{relative_file_path}' escapes skill directory '{skill_name}'."
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error reading skill '{skill_name}/{relative_file_path}': {exc}"
