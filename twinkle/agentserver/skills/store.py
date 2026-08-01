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


def _strip_yaml_quotes(v: str) -> str:
    """剥掉 YAML 标量值的包围引号("foo"→foo,'foo'→foo)。块标量内容不含引号,不调此。"""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1].strip()
    return v


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """解析 --- 包围的 frontmatter 为 {key: value}。单行值(剥包围引号);支持用 YAML 块标量
    (``|`` 字面 / ``>`` 折叠)的多行值——缩进行折叠成一行(适合作一句话 description)。
    无闭合 --- 返 None。无 PyYAML 依赖。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, str] = {}
    i = 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "---":
            return out
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        indicator = value.strip()
        if indicator in ("|", "|-", "|+", ">", ">-", ">+"):
            # 块标量:收集后续缩进/空行(比 key 行深)为内容,折叠成一行。
            block: list[str] = []
            i += 1
            while i < n:
                bline = lines[i]
                if bline.strip() == "---":
                    break
                if bline.startswith((" ", "\t")) or bline.strip() == "":
                    if bline.strip():
                        block.append(bline.strip())
                    i += 1
                else:
                    break  # 回到 key 层缩进 → 块结束
            out[key] = " ".join(block).strip()
            continue
        out[key] = _strip_yaml_quotes(indicator)
        i += 1
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

    def __init__(self, skills_dir: str, enabled_skills: list[str] | None = None) -> None:
        self._dir = Path(skills_dir)
        self._enabled_skills: set[str] | None = set(enabled_skills) if enabled_skills else None
        self._mtime_signature: tuple = ()
        self._skills: list[Skill] = []

    def list_skills(self) -> list[Skill]:
        self._refresh_if_changed()
        return self._skills

    def get_skill(self, name: str) -> Skill | None:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        return None

    def _refresh_if_changed(self) -> None:
        signature = self._build_mtime_signature()
        if signature != self._mtime_signature:
            self._skills = self._scan()
            self._mtime_signature = signature

    def _build_mtime_signature(self) -> tuple:
        """每个子目录的 (name, SKILL.md.mtime) —— 内容编辑 + 增删子目录都触发重扫。"""
        if not self._dir.is_dir():
            return ()
        entries: list[tuple] = []
        for sub in sorted(self._dir.iterdir()):
            if not sub.is_dir():
                continue
            skill_md = sub / "SKILL.md"
            try:
                entries.append((sub.name, skill_md.stat().st_mtime))
            except OSError:
                entries.append((sub.name, -1.0))
        return tuple(entries)

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
            if self._enabled_skills is not None and skill.name not in self._enabled_skills:
                continue
            out.append(skill)
        return out
