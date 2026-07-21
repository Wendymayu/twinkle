"""Smoke-test the observability pipeline against a local OTLP collector.

Drives a REAL AgentLoop / LLMClient / ToolManager (instrumented via
apply_instrumentors) with a FAKE openai client — no API key needed — so the
monkey-patched choke points emit real spans, exported via OTLP/gRPC to
http://localhost:4317 (e.g. Labubu, UI at http://localhost:8080).

Expected trace tree in the collector UI:
  twinkle.agent.invoke
  ├─ gen_ai.chat   (turn 1: model decides to call the echo tool)
  ├─ gen_ai.tool    (echo)
  └─ gen_ai.chat   (turn 2: final answer)

Run:  python scripts/obs_smoke.py
"""
from __future__ import annotations

import asyncio

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.session_store import SessionStore
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.observability.config import load_config
from twinkle.observability.instrumentors import apply_instrumentors
from twinkle.observability.metrics import Metrics

ENDPOINT = "http://localhost:4317"


# --- fake openai streaming shapes (mirrors tests/test_llm_client.py) ---
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


@tool
async def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


class _Env:
    """Duck-typed envelope (agent_loop reads .request_id/.session_id/.params)."""

    def __init__(self):
        self.request_id = "smoke-1"
        self.session_id = "smoke-sess"
        self.method = "chat"
        self.params = {"query": "please echo hello"}


async def main() -> None:
    cfg = load_config()
    resource = Resource.create({"service.name": cfg.service_name})
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
    )
    tracer = tp.get_tracer("twinkle")
    # Metrics(None) -> all instruments fail-soft to no-op (smoke focuses on traces).
    apply_instrumentors(tracer, Metrics(None), cfg)

    # Turn 1: model emits a tool_call for echo; Turn 2: final answer.
    scripts = [
        [  # turn 1 — accumulate a tool_call, then finish_reason=tool_calls
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, id="call_1", name="echo", arguments="")]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='{"text":"hello"}')]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
            _Chunk([], usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
        ],
        [  # turn 2 — final text + stop
            _Chunk([_Choice(_Delta(content="done"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
            _Chunk([], usage={"prompt_tokens": 14, "completion_tokens": 1, "total_tokens": 15}),
        ],
    ]

    llm = LLMClient(base_url="x", api_key="y", model="smoke-model", client=_FakeClient(scripts))
    tools = ToolManager()
    tools.register(echo)
    loop = AgentLoop(llm=llm, store=SessionStore(), tools=tools, memory=LongTermMemory())

    print(f"twinkle obs smoke -> OTLP/gRPC {ENDPOINT}")
    async for frame in loop.run_stream(_Env()):
        print(f"  frame: {frame.response_kind} status={frame.status}")

    tp.force_flush(6000)
    tp.shutdown()
    print("done. Check the collector UI (e.g. Labubu http://localhost:8080) for the trace tree:")
    print("  twinkle.agent.invoke -> gen_ai.chat (x2) + gen_ai.tool")


if __name__ == "__main__":
    asyncio.run(main())
