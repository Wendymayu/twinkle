import asyncio

from twinkle.agentserver.context_compression import estimate_tokens
from twinkle.agentserver.hooks.base import ModelCallInputs
from twinkle.agentserver.hooks.builtin import ContextCompressionHook
from twinkle.agentserver.llm_client import TextDelta


class _SummaryLLM:
    """Fake LLM for the summary call inside compress_messages. Yields fixed text."""
    def __init__(self, summary="摘要内容"):
        self._summary = summary

    async def stream(self, messages, tools):
        yield TextDelta(self._summary)


class _RaisingLLM:
    """stream() raises — exercises compress_messages' degrade-to-head+tail path."""
    async def stream(self, messages, tools):
        raise RuntimeError("boom")
        yield  # makes stream an async generator (unreachable; changes semantics so async-for gets an async gen that raises on first __anext__, not a bare coroutine)


class _Ctx:
    """Minimal ctx stub: hook only touches ctx.inputs.messages."""
    def __init__(self, messages):
        self.inputs = ModelCallInputs(messages=messages, tools=[])


def _big_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    return msgs


def test_compresses_over_threshold_and_assigns_back():
    hook = ContextCompressionHook(
        llm=_SummaryLLM(), token_threshold=1, keep_recent_pairs=2, summary_prompt="p")
    big = _big_messages()
    ctx = _Ctx(big)

    asyncio.run(hook.before_model_call(ctx))

    contents = [m.get("content", "") for m in ctx.inputs.messages]
    assert any("[prior context summary]" in c for c in contents)
    assert estimate_tokens(ctx.inputs.messages) < estimate_tokens(big)
    assert not any("[prior context summary]" in m.get("content", "") for m in big)


def test_noop_under_threshold():
    hook = ContextCompressionHook(
        llm=_SummaryLLM(), token_threshold=60_000, keep_recent_pairs=6, summary_prompt="p")
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    ctx = _Ctx(small)

    asyncio.run(hook.before_model_call(ctx))

    assert not any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
    assert ctx.inputs.messages == small
    assert ctx.inputs.messages is not small


def test_degrades_to_head_tail_on_llm_failure():
    hook = ContextCompressionHook(
        llm=_RaisingLLM(), token_threshold=1, keep_recent_pairs=2, summary_prompt="p")
    ctx = _Ctx(_big_messages())

    asyncio.run(hook.before_model_call(ctx))

    assert not any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
    assert ctx.inputs.messages[0]["role"] == "system"


def test_uses_config_defaults_when_no_override(monkeypatch):
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "CONTEXT_TOKEN_THRESHOLD", 1)
    monkeypatch.setattr(twinkle.config, "CONTEXT_KEEP_RECENT_PAIRS", 2)
    monkeypatch.setattr(twinkle.config, "CONTEXT_SUMMARY_PROMPT", "p")
    hook = ContextCompressionHook(llm=_SummaryLLM())
    ctx = _Ctx(_big_messages())

    asyncio.run(hook.before_model_call(ctx))

    assert any("[prior context summary]" in m.get("content", "") for m in ctx.inputs.messages)
