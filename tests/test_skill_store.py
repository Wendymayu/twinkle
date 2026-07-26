from pathlib import Path
from twinkle.agentserver.skills.store import Skill, parse_skill_md, parse_frontmatter


def test_parse_frontmatter_basic():
    text = "---\nname: doc-audit\ndescription: 核对文档与源码一致。\ntrigger: 用户说刷文档时加载。\n---\n\n## 正文\n"
    fm = parse_frontmatter(text)
    assert fm["name"] == "doc-audit"
    assert fm["description"] == "核对文档与源码一致。"
    assert fm["trigger"] == "用户说刷文档时加载。"


def test_parse_frontmatter_value_with_colon():
    # value 含冒号(1:1 对齐)——partition 在第一个冒号切,value 保留其余
    text = "---\ndescription: 文档 1:1 对齐。\n---\n"
    assert parse_frontmatter(text)["description"] == "文档 1:1 对齐。"


def test_parse_frontmatter_no_fence_returns_none():
    assert parse_frontmatter("no frontmatter here") is None


def test_parse_frontmatter_no_close_returns_none():
    assert parse_frontmatter("---\nname: x\n") is None


def test_parse_skill_md(tmp_path):
    d = tmp_path / "doc-audit"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: doc-audit\ndescription: 核对文档。\ntrigger: 关键词时加载。\n---\n\n## 流程\n",
        encoding="utf-8",
    )
    skill = parse_skill_md(d)
    assert skill is not None
    assert skill.name == "doc-audit"
    assert skill.description == "核对文档。"
    assert skill.directory == d.resolve()


def test_parse_skill_md_missing_name_returns_none(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name\n---\n", encoding="utf-8")
    assert parse_skill_md(d) is None


def test_parse_skill_md_no_file_returns_none(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert parse_skill_md(d) is None
