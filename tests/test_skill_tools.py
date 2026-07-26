import asyncio
from pathlib import Path
import pytest
from twinkle.agentserver.skills import _set_skill_manager, SkillManager


def _make_skill(dir_: Path, name: str, desc: str, body: str = "body") -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8"
    )


@pytest.fixture
def isolated_skills(tmp_path):
    _make_skill(tmp_path / "a", "a", "desc a", "## A flow")
    _make_skill(tmp_path / "b", "b", "desc b")
    _set_skill_manager(SkillManager(str(tmp_path)))
    yield tmp_path
    _set_skill_manager(None)


def test_list_skill_catalog(isolated_skills):
    from twinkle.agentserver.tools.builtin.skill_tools import list_skill
    out = asyncio.run(list_skill.func())
    assert "## 可用技能" in out
    assert "a: desc a" in out
    assert "b: desc b" in out


def test_list_skill_empty(tmp_path):
    _set_skill_manager(SkillManager(str(tmp_path)))
    try:
        from twinkle.agentserver.tools.builtin.skill_tools import list_skill
        assert asyncio.run(list_skill.func()) == "No skills available."
    finally:
        _set_skill_manager(None)


def test_read_skill_body(isolated_skills):
    from twinkle.agentserver.tools.builtin.skill_tools import read_skill
    out = asyncio.run(read_skill.func("a"))
    assert "## A flow" in out  # SKILL.md 正文


def test_read_skill_not_found(isolated_skills):
    from twinkle.agentserver.tools.builtin.skill_tools import read_skill
    out = asyncio.run(read_skill.func("nope"))
    assert "not found" in out.lower()


def test_read_skill_relative_file(isolated_skills):
    from twinkle.agentserver.tools.builtin.skill_tools import read_skill
    # 读默认 SKILL.md(同 test_read_skill_body);验证 relative_file_path 参数可用
    out = asyncio.run(read_skill.func("a", "SKILL.md"))
    assert "## A flow" in out
