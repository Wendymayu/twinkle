# tests/test_runtime_env_hook.py
"""RuntimeEnvHook — before_model_call 把 today/os 放 ctx.extra['environment_context']。

priority 99 最先跑;不进 system prompt(用 ctx.extra 不用 ctx.builder);
loop 端 pop() 消费防多轮累积。
"""
import asyncio
import platform
import datetime

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.runtime_env_hook import RuntimeEnvHook


def _ctx():
    return HookContext(
        agent=None, event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[]),
        session_id="s", request_id="r",
    )


def test_env_goes_to_extra_not_messages():
    ctx = _ctx()
    asyncio.run(RuntimeEnvHook().before_model_call(ctx))
    # messages 不变(env 不进 system)
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
    # env 在 extra
    env = ctx.extra.get("environment_context")
    assert env and len(env) == 1
    content = env[0]["content"]
    assert datetime.date.today().isoformat() in content
    assert ctx.extra["environment_context"][0]["source"] == "runtime_env"


def test_priority_is_99():
    assert RuntimeEnvHook.priority == 99


def test_loop_pop_prevents_accumulation_across_steps():
    """模拟 loop 两步:每步 RuntimeEnvHook append,loop 端 pop,不累积。"""
    ctx = _ctx()
    hook = RuntimeEnvHook()
    for _ in range(2):
        asyncio.run(hook.before_model_call(ctx))
        # loop 端消费
        env = ctx.extra.pop("environment_context", None)
        assert env and len(env) == 1
    # 第二步 pop 后 extra 无残留
    assert "environment_context" not in ctx.extra
