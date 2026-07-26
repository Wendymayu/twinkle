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


class SkillManager:
    """扫 <skills_dir>/<name>/SKILL.md,mtime 热重载,可选白名单。坏 skill 跳过不崩。"""

    def __init__(self, skills_dir: str, enabled: list[str] | None = None) -> None:
        self._dir = Path(skills_dir)
        self._enabled: set[str] | None = set(enabled) if enabled else None
        self._sig: tuple = ()
        self._skills: list[Skill] = []

    def list_skills(self) -> list[Skill]:
        self._refresh_if_changed()
        return self._skills

    def get_skill(self, name: str) -> Skill | None:
        for s in self.list_skills():
            if s.name == name:
                return s
        return None

    def _refresh_if_changed(self) -> None:
        sig = self._build_signature()
        if sig != self._sig:
            self._skills = self._scan()
            self._sig = sig

    def _build_signature(self) -> tuple:
        """每个子目录的 (name, SKILL.md.mtime) —— 内容编辑 + 增删子目录都触发重扫。"""
        if not self._dir.is_dir():
            return ()
        sigs: list[tuple] = []
        for sub in sorted(self._dir.iterdir()):
            if not sub.is_dir():
                continue
            skill_md = sub / "SKILL.md"
            try:
                sigs.append((sub.name, skill_md.stat().st_mtime))
            except OSError:
                sigs.append((sub.name, -1.0))
        return tuple(sigs)

    def _scan(self) -> list[Skill]:
        if not self._dir.is_dir():
            return []
        out: list[Skill] = []
        for sub in sorted(self._dir.iterdir()):
            if not sub.is_dir():
                continue
            skill = parse_skill_md(sub)
            if skill is None:
                continue
            if self._enabled is not None and skill.name not in self._enabled:
                continue
            out.append(skill)
        return out
