# tests/test_base_sections.py
"""normal/leader/member base_sections 工厂——返回 list[PromptSection],priority 10,内容=对应 build_* prompt。"""
from twinkle.agentserver.agent import (
    normal_base_sections, leader_base_sections, member_base_sections,
    build_system_prompt, build_leader_system_prompt, build_member_system_prompt,
)
from twinkle.agentserver.prompts import PromptSection


def test_normal_base_sections():
    secs = normal_base_sections()
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert secs[0].content == build_system_prompt()


def test_leader_base_sections():
    secs = leader_base_sections()
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert secs[0].content == build_leader_system_prompt()


def test_member_base_sections_bakes_persona():
    secs = member_base_sections(persona="数据分析师", workspace="/shared", member_name="analyst")
    assert len(secs) == 1
    assert secs[0].name == "system_prompt"
    assert secs[0].priority == 10
    assert "数据分析师" in secs[0].content
    assert "/shared" in secs[0].content
    assert secs[0].content == build_member_system_prompt(
        persona="数据分析师", workspace="/shared", member_name="analyst")
