import asyncio

from twinkle.agentserver import agent_loop
from twinkle.agentserver.context_compression import estimate_tokens
from twinkle.agentserver.hooks.builtin import ContextCompressionHook
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.e2a.models import E2AEnvelope


class _Store:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def get_messages(self, sid):
        return list(self._msgs)

    async def append(self, sid, message, request_id=None, event_type=None):
        self._msgs.append(dict(message))


class _Tools:
    def schemas(self):
        return []

    async def execute(self, name, args):
        return ""


class _LLM:
    """Records the last messages received via stream(); returns ok."""

    def __init__(self):
        self.seen = None

    async def stream(self, messages, tools):
        self.seen = messages
        yield TextDelta("ok")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "ok", "tool_calls": None},
        )


def test_run_stream_compresses_before_llm():
    big = [{"role": "system", "content": "s"}]
    big += [{"role": "user", "content": f"turn{i} " + "x" * 200} for i in range(20)]
    store = _Store(big)
    real_llm = _LLM()
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())
    loop.register_hook(ContextCompressionHook(
        llm=real_llm, token_threshold=1, keep_recent_pairs=2, summary_prompt="p"))

    env = E2AEnvelope(
        request_id="r1", session_id="s1", method="chat.send", params={"query": "hi"}
    )
    frames = []

    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)

    asyncio.run(collect())
    assert real_llm.seen is not None
    assert estimate_tokens(real_llm.seen) < estimate_tokens(big)
    assert real_llm.seen[0]["role"] == "system"  # head 保留


def test_run_stream_no_compress_under_threshold():
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    store = _Store(small)
    real_llm = _LLM()
    loop = agent_loop.AgentLoop(llm=real_llm, store=store, tools=_Tools())
    loop.register_hook(ContextCompressionHook(
        llm=real_llm, token_threshold=60_000, keep_recent_pairs=6, summary_prompt="p"))

    env = E2AEnvelope(
        request_id="r2", session_id="s2", method="chat.send", params={"query": "yo"}
    )
    frames = []

    async def collect():
        async for f in loop.run_stream(env):
            frames.append(f)

    asyncio.run(collect())
    assert real_llm.seen is not None
    # Under threshold: no summary message inserted
    assert not any("[prior context summary]" in m.get("content", "") for m in real_llm.seen)
    assert frames and frames[-1].response_kind == "e2a.complete"
