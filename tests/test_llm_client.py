import asyncio

from twinkle.agentserver.llm_client import LLMClient, TextDelta, Finish


# --- fake openai streaming shapes (mirrors openai SDK chunk objects) ---
class _Func:
    def __init__(self, name=None, arguments=""):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=""):
        self.index = index
        self.id = id
        self.function = _Func(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class _FakeCompletions:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def create(self, **kwargs):
        chunks = self._scripts[self.calls]
        self.calls += 1

        async def gen():
            for c in chunks:
                yield c

        return gen()


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, scripts):
        self.chat = _FakeChat(_FakeCompletions(scripts))


def _run(coro):
    return asyncio.run(coro)


def test_text_stream_emits_deltas_then_finish() -> None:
    scripts = [
        [
            _Chunk([_Choice(_Delta(content="hel"))]),
            _Chunk([_Choice(_Delta(content="lo"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        events = [e async for e in client.stream(messages=[{"role": "user", "content": "hi"}], tools=[])]
        return events

    events = _run(run())
    assert isinstance(events[0], TextDelta) and events[0].content == "hel"
    assert isinstance(events[1], TextDelta) and events[1].content == "lo"
    assert isinstance(events[2], Finish)
    assert events[2].finish_reason == "stop"
    assert events[2].assistant_message == {"role": "assistant", "content": "hello", "tool_calls": None}


def test_trailing_empty_choices_chunk_does_not_crash() -> None:
    # dashscope / openai (with stream_options.include_usage) send a final
    # usage-only chunk with choices=[]; the client must skip it, not index [0].
    scripts = [
        [
            _Chunk([_Choice(_Delta(content="hi"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
            _Chunk([]),  # usage-only trailing chunk
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        return [e async for e in client.stream(messages=[{"role": "user", "content": "hi"}], tools=[])]

    events = _run(run())
    assert isinstance(events[0], TextDelta) and events[0].content == "hi"
    assert isinstance(events[1], Finish)
    assert events[1].finish_reason == "stop"
    assert events[1].assistant_message["content"] == "hi"


def test_tool_call_fragments_accumulated() -> None:
    scripts = [
        [
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, id="call_1", name="web_fetch", arguments="")]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='{"url":')]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='"http://x"}')]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        events = [e async for e in client.stream(messages=[{"role": "user", "content": "fetch"}], tools=[])]
        return events

    events = _run(run())
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.finish_reason == "tool_calls"
    tcs = finish.assistant_message["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_1"
    assert tcs[0]["function"]["name"] == "web_fetch"
    assert tcs[0]["function"]["arguments"] == '{"url":"http://x"}'


def test_trailing_usage_chunk_is_captured_on_finish() -> None:
    usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    scripts = [
        [
            _Chunk([_Choice(_Delta(content="hi"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
            _Chunk([], usage=usage),  # usage-only trailing chunk
        ]
    ]
    client = LLMClient(base_url="x", api_key="y", model="m", client=_FakeClient(scripts))

    async def run():
        return [e async for e in client.stream(messages=[{"role": "user", "content": "hi"}], tools=[])]

    events = _run(run())
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.usage == usage
