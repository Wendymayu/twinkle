import asyncio
from pathlib import Path
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook
from twinkle.agentserver.skills import _set_skill_manager, SkillManager


def _make_skill(dir_: Path, name: str, desc: str) -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def _ctx(messages=None) -> HookContext:
    return HookContext(
        agent=None, event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=messages or [], tools=[]),
        session_id="s", request_id="r",
    )


@pytest.fixture
def isolated_skills(tmp_path):
    _make_skill(tmp_path / "a", "a", "desc a")
    _make_skill(tmp_path / "b", "b", "desc b")
    _set_skill_manager(SkillManager(str(tmp_path)))
    yield tmp_path
    _set_skill_manager(None)


def test_all_mode_prepends_catalog(isolated_skills):
    hook = SkillHook(mode="all")
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(hook.before_model_call(ctx))
    assert ctx.inputs.messages[0]["role"] == "system"
    assert "## 可用技能" in ctx.inputs.messages[0]["content"]
    assert "a: desc a" in ctx.inputs.messages[0]["content"]
    # 原 messages 保留在后
    assert ctx.inputs.messages[1] == {"role": "user", "content": "hi"}


def test_all_mode_replaces_list_not_mutate(isolated_skills):
    """不 in-place mutate 原 list(避免污染 store 内部 list)。"""
    original = [{"role": "user", "content": "hi"}]
    ctx = _ctx(original)
    asyncio.run(SkillHook(mode="all").before_model_call(ctx))
    assert original == [{"role": "user", "content": "hi"}]  # 原 list 未被改
    assert ctx.inputs.messages is not original  # 赋了新 list


def test_auto_list_mode_prepends_note(isolated_skills):
    hook = SkillHook(mode="auto_list")
    ctx = _ctx([])
    asyncio.run(hook.before_model_call(ctx))
    assert ctx.inputs.messages[0]["role"] == "system"
    assert "list_skill" in ctx.inputs.messages[0]["content"]


def test_no_skills_is_noop(tmp_path):
    _set_skill_manager(SkillManager(str(tmp_path)))  # 空目录
    try:
        ctx = _ctx([{"role": "user", "content": "hi"}])
        asyncio.run(SkillHook(mode="all").before_model_call(ctx))
        assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]  # 未动
    finally:
        _set_skill_manager(None)
