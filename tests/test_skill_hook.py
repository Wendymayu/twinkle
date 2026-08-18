import asyncio
from pathlib import Path
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.prompts import PromptSection
from twinkle.agentserver.skills import _set_skill_manager, SkillManager


def _make_skill(dir_: Path, name: str, desc: str) -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def _ctx(query="hi") -> HookContext:
    """before_invoke 时 builder 尚不存在(每步在 loop 里才建);inputs 是 InvokeInputs。"""
    return HookContext(
        agent=None, event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query=query, mode=""),
        session_id="s", request_id="r",
    )


def _skills_section(ctx: HookContext) -> PromptSection | None:
    secs = ctx.extra.get("frozen_sections", [])
    return next((s for s in secs if s.name == "skills"), None)


@pytest.fixture
def isolated_skills(tmp_path):
    _make_skill(tmp_path / "a", "a", "desc a")
    _make_skill(tmp_path / "b", "b", "desc b")
    _set_skill_manager(SkillManager(str(tmp_path)))
    yield tmp_path
    _set_skill_manager(None)


def test_all_mode_stashes_catalog_section(isolated_skills):
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    sec = _skills_section(ctx)
    assert sec is not None
    assert sec.priority == 90
    assert "## 可用技能" in sec.content
    assert "a: desc a" in sec.content
    assert "b: desc b" in sec.content


def test_auto_list_mode_stashes_note_section(isolated_skills):
    hook = SkillHook(mode="auto_list")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    sec = _skills_section(ctx)
    assert sec is not None
    assert "list_skill" in sec.content


def test_stashes_exactly_one_skills_section(isolated_skills):
    """同名覆写语义在 frozen_sections 上仍只留一条 skills section。"""
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    skills_secs = [s for s in ctx.extra.get("frozen_sections", []) if s.name == "skills"]
    assert len(skills_secs) == 1


def test_no_skills_is_noop(tmp_path):
    _set_skill_manager(SkillManager(str(tmp_path)))  # 空目录
    try:
        ctx = _ctx()
        asyncio.run(SkillHook(mode="all").before_invoke(ctx))
        assert "frozen_sections" not in ctx.extra  # 无 skill → 不 stash
    finally:
        _set_skill_manager(None)


def test_before_invoke_does_not_touch_builder(isolated_skills):
    """before_invoke 在 builder 存在前跑;不应碰 ctx.builder(仍为 None)。"""
    hook = SkillHook(mode="all")
    ctx = _ctx()
    asyncio.run(hook.before_invoke(ctx))
    assert ctx.builder is None
