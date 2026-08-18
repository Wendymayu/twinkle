# tests/test_prompt_env_strip.py
"""build_*_system_prompt 去掉 today/os env 数值行(env 移尾部 RuntimeEnvHook)。

断言用带全角冒号的 "当前平台：" / "当前日期：" 精确匹配 env 数值行(已删),
不误伤引导句"必须严格使用与当前平台匹配的命令语法"(保留,无冒号)。
"""
from twinkle.agentserver.agent import (
    build_system_prompt, build_agent_runtime_prompt, build_leader_system_prompt,
)


def test_base_prompt_no_env_values():
    p = build_system_prompt()
    assert "当前平台：" not in p
    assert "当前日期：" not in p
    assert "运行环境" in p  # 块头保留
    assert "身份与行为原则" in p  # identity 保留


def test_runtime_prompt_no_env_values():
    p = build_agent_runtime_prompt()
    assert "当前平台：" not in p
    assert "当前日期：" not in p
    assert "运行环境" in p


def test_leader_prompt_no_env_values():
    p = build_leader_system_prompt()
    assert "当前平台：" not in p
    assert "当前日期：" not in p
    assert "运行环境" in p
    assert "TeamLeader" in p
