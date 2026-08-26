"""针对本地 OTLP collector 的可观测性链路冒烟测试。

用 FAKE openai client 驱动 REAL ReActAgent / LLMClient / ToolManager（经
apply_instrumentors 插桩）——无需 API key——使 monkey-patch 的埋点发出真实 span，
经 OTLP/gRPC 导出到 http://localhost:4317（如 Labubu，UI 在 http://localhost:8080）。

collector UI 中预期的 trace 树：
  twinkle.agent.invoke
  ├─ gen_ai.chat   （turn 1：模型决定调用 echo tool）
  ├─ gen_ai.tool    （echo）
  └─ gen_ai.chat   （turn 2：最终回答）

运行：python scripts/obs_smoke.py
"""
from __future__ import annotations

import asyncio

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from twinkle.agentserver.agent import AgentRequest, ReActAgent
from twinkle.agentserver.llm_client import LLMClient
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager
from twinkle.config import SESSIONS_DIR
from twinkle.observability.config import load_config
from twinkle.observability.instrumentors import apply_instrumentors
from twinkle.observability.metrics import Metrics

ENDPOINT = "http://localhost:4317"


# --- fake openai 流式分片形状（镜像 tests/test_llm_client.py） ---
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
    """回显 text。"""
    return f"echo: {text}"



async def main() -> None:
    cfg = load_config()
    resource = Resource.create({"service.name": cfg.service_name})
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
    )
    tracer = tp.get_tracer("twinkle")
    # Metrics(None) -> 所有 instrument 软失败为 no-op（冒烟只关注 trace）。
    apply_instrumentors(tracer, Metrics(None), cfg)

    # Turn 1：模型发出对 echo 的 tool_call；Turn 2：最终回答。
    scripts = [
        [  # turn 1 — 累积一个 tool_call，随后 finish_reason=tool_calls
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, id="call_1", name="echo", arguments="")]))]),
            _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='{"text":"hello"}')]))]),
            _Chunk([_Choice(_Delta(), finish_reason="tool_calls")]),
            _Chunk([], usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
        ],
        [  # turn 2 — 最终文本 + stop
            _Chunk([_Choice(_Delta(content="done"))]),
            _Chunk([_Choice(_Delta(), finish_reason="stop")]),
            _Chunk([], usage={"prompt_tokens": 14, "completion_tokens": 1, "total_tokens": 15}),
        ],
    ]

    llm = LLMClient(base_url="x", api_key="y", model="smoke-model")
    llm._client = _FakeClient(scripts)  # 脚本化的 fake（无真实 OpenAI 调用）——仅冒烟注入
    tools = ToolManager()
    tools.register(echo)
    loop = ReActAgent(llm=llm, store=SessionStore(SESSIONS_DIR), tools=tools)

    print(f"twinkle obs smoke -> OTLP/gRPC {ENDPOINT}")
    request = AgentRequest(session_id="smoke-sess", request_id="smoke-1",
                           query="please echo hello")
    async for frame in loop.run(request):
        print(f"  frame: {frame.response_kind} status={frame.status}")

    tp.force_flush(6000)
    tp.shutdown()
    print("done. Check the collector UI (e.g. Labubu http://localhost:8080) for the trace tree:")
    print("  twinkle.agent.invoke -> gen_ai.chat (x2) + gen_ai.tool")


if __name__ == "__main__":
    asyncio.run(main())
