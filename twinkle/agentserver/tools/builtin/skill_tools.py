"""Skill 工具 — 模型用 list_skill 看清单、read_skill 载入 SKILL.md 指令体。

read_skill 把 SKILL.md 正文作 tool_result 回灌(标准 {role:tool}),不抛、不炸 ReAct
(错误走字符串,对齐 todo_tools 的 TodoError 模式)。复用普通文件读(skill 是本地文件)。
evolution 开启时,read_skill(SKILL.md) 会把该 skill top-3 高分经验正文拼进返回值
(让经验正文随 tool_result 进上下文,不扰前缀 cache;脚本类只取 summary)。
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


def _append_top_experiences(skill_name: str, content: str) -> str:
    """read_skill(SKILL.md) 时把该 skill top-3 高分经验正文拼进返回值。

    让经验正文随 tool_result 进上下文(不扰前缀 cache),模型一次 read 即得 SKILL.md + top-N 正文,
    不必再 read sidecar。脚本类记录只取 summary(源码是引用串,模型要再 read 脚本文件)。
    fail-soft：evolution 关 / 无经验 / 读取异常 → 返回原文。
    注意：top-N 的 N 必须与 evolution_hook.after_tool_call 记 presented 的 limit 一致(均 3)。
    """
    try:
        from twinkle.config import EVOLUTION_ENABLED
        if not EVOLUTION_ENABLED:
            return content
        from twinkle.agentserver.evolution import get_evolution_store
        records = get_evolution_store().get_records_by_score(skill_name, limit=3)
        if not records:
            return content
    except Exception:
        return content

    lines = [
        "",
        "<!-- evolution-experiences-start -->",
        "## Evolution Experiences (top 3, auto-injected)",
    ]
    for r in records:
        if r.change.action == "skip":
            continue
        lines.append(f"### [{r.id}] {r.summary or r.change.section or 'experience'}")
        if r.change.target == "script":
            lines.append(f"(脚本经验) {r.summary or ''} — 详见 evolution/scripts/")
        else:
            lines.append((r.change.content or "")[:500])
        lines.append("")
    lines.append("<!-- evolution-experiences-end -->")
    return content.rstrip() + "\n" + "\n".join(lines) + "\n"


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
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error reading skill '{skill_name}/{relative_file_path}': {exc}"
    # 读 SKILL.md 时拼接 top-N 高分经验正文(让正文进上下文,见 evolution_hook.after_tool_call)
    if relative_file_path == "SKILL.md":
        content = _append_top_experiences(skill_name, content)
    return content
