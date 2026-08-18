# tests/test_hook_context_builder.py
"""HookContext.builder 字段——loop 每步赋 builder,hook 从 ctx.builder.add_section。"""
from twinkle.agentserver.hooks.base import HookContext, HookEvent, InvokeInputs
from twinkle.agentserver.prompts import PromptSection, SystemPromptBuilder


def _ctx():
    return HookContext(
        agent=None, event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q"), session_id="s", request_id="r",
    )


def test_builder_defaults_none():
    assert _ctx().builder is None


def test_builder_settable_and_usable():
    ctx = _ctx()
    ctx.builder = SystemPromptBuilder()
    ctx.builder.add_section(PromptSection("skills", "S", priority=90))
    assert ctx.builder.build() == "S"
