from pathlib import Path
from twinkle.agentserver.skills.store import Skill, SkillManager, parse_skill_md, parse_frontmatter


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


def _make_skill(dir_: Path, name: str, desc: str = "d") -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def test_skill_manager_scans(tmp_path):
    _make_skill(tmp_path / "a", "a")
    _make_skill(tmp_path / "b", "b")
    mgr = SkillManager(str(tmp_path))
    names = sorted(s.name for s in mgr.list_skills())
    assert names == ["a", "b"]


def test_skill_manager_empty_dir(tmp_path):
    assert SkillManager(str(tmp_path)).list_skills() == []


def test_skill_manager_missing_dir(tmp_path):
    assert SkillManager(str(tmp_path / "nope")).list_skills() == []


def test_skill_manager_skips_malformed(tmp_path):
    _make_skill(tmp_path / "a", "a")
    bad = tmp_path / "bad"; bad.mkdir()
    (bad / "SKILL.md").write_text("---\ndescription: no name\n---\n", encoding="utf-8")
    assert [s.name for s in SkillManager(str(tmp_path)).list_skills()] == ["a"]


def test_skill_manager_whitelist(tmp_path):
    _make_skill(tmp_path / "a", "a")
    _make_skill(tmp_path / "b", "b")
    mgr = SkillManager(str(tmp_path), enabled=["a"])
    assert [s.name for s in mgr.list_skills()] == ["a"]


def test_skill_manager_mtime_reload(tmp_path):
    _make_skill(tmp_path / "a", "a", "v1")
    mgr = SkillManager(str(tmp_path))
    assert mgr.list_skills()[0].description == "v1"
    # 改 SKILL.md 内容(mtime 变)→ 重新扫到新描述
    (tmp_path / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: v2\n---\n\nbody\n", encoding="utf-8"
    )
    assert mgr.list_skills()[0].description == "v2"


def test_skill_manager_add_skill_dir(tmp_path):
    _make_skill(tmp_path / "a", "a")
    mgr = SkillManager(str(tmp_path))
    assert len(mgr.list_skills()) == 1
    _make_skill(tmp_path / "b", "b")  # 新增子目录 → 签名变 → 重扫
    assert len(mgr.list_skills()) == 2


def test_skill_manager_get_skill(tmp_path):
    _make_skill(tmp_path / "a", "a")
    mgr = SkillManager(str(tmp_path))
    assert mgr.get_skill("a") is not None
    assert mgr.get_skill("nope") is None
