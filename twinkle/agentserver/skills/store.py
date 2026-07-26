"""Skill 系统 — Skill 模型 + SkillManager(扫描/mtime 热重载/白名单)。

一个 skill = <SKILLS_DIR>/<name>/SKILL.md(YAML frontmatter name/description/trigger
+ markdown 指令体)。trigger 解析后丢弃(模型靠 description 自己选,不做关键词自动匹配,
对齐 jiuwenswarm)。frontmatter 用 hand-rolled 极简解析器(单行值,无 PyYAML 依赖)。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str                 # skill 名,唯一 key
    description: str          # 给模型看的一句话描述
    directory: Path           # skill 目录绝对路径(读 SKILL.md / 附带文件用)


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """解析 --- 包围的 frontmatter 为 {key: value}。单行值;无闭合 --- 返 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return None  # 没遇到闭合 ---


def parse_skill_md(directory: Path) -> Skill | None:
    """解析 directory/SKILL.md 成 Skill。缺 name/description、无文件、坏 frontmatter → None。"""
    skill_md = directory / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = parse_frontmatter(text)
    if fm is None:
        return None
    name = fm.get("name")
    description = fm.get("description")
    if not name or not description:
        return None
    return Skill(name=name, description=description, directory=directory.resolve())
