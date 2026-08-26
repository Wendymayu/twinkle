import pytest

# 若未安装 [obs] 则优雅跳过整个文件 —— 在没有 opentelemetry 的情况下保持
# 现有测试套件全绿。
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)

from twinkle.observability import attributes as A


class CollectingSpanExporter(SpanExporter):
    """内存版 SpanExporter；通过 .spans 访问已追加的 span。"""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        return True

    def force_flush(self, timeout_millis=30000):
        return True


_RESOURCE = Resource.create({"service.name": "twinkle-test"})


@pytest.fixture
def tracer_exporter():
    exp = CollectingSpanExporter()
    provider = TracerProvider(resource=_RESOURCE)
    provider.add_span_processor(SimpleSpanProcessor(exp))
    tracer = provider.get_tracer("twinkle-test")
    yield tracer, exp
    provider.force_flush()
    provider.shutdown()


@pytest.fixture
def meter_metricreader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], resource=_RESOURCE)
    meter = provider.get_meter("twinkle-test")
    yield meter, reader
    provider.force_flush()
    provider.shutdown()


def test_attribute_constants_are_strings():
    assert A.SPAN_AGENT_INVOKE == "twinkle.agent.invoke"
    assert A.SPAN_GEN_AI_CHAT == "gen_ai.chat"
    assert A.SPAN_GEN_AI_TOOL == "gen_ai.tool"
    assert A.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert A.METRIC_TOKEN_USAGE == "gen_ai.client.token.usage"
    assert A.TOOL_ERROR_PREFIX == "[tool error]"
    # --- 新增：compression + evolution ---
    assert A.SPAN_COMPRESSION == "twinkle.compression"
    assert A.SPAN_SKILL_EVOLUTION == "twinkle.skill.evolution"
    assert A.TWINKLE_COMPRESSION_TOKENS_BEFORE == "twinkle.compression.tokens_before"
    assert A.TWINKLE_COMPRESSION_TOKENS_AFTER == "twinkle.compression.tokens_after"
    assert A.TWINKLE_COMPRESSION_COMPRESSED == "twinkle.compression.compressed"
    assert A.TWINKLE_COMPRESSION_HAS_SUMMARY == "twinkle.compression.has_summary"
    assert A.TWINKLE_COMPRESSION_STRATEGY == "twinkle.compression.strategy"
    assert A.TWINKLE_SKILL_NAME == "twinkle.skill.name"
    assert A.TWINKLE_EVOLUTION_STATUS == "twinkle.evolution.status"
    assert A.TWINKLE_EVOLUTION_MESSAGE == "twinkle.evolution.message"


import asyncio
import types

from twinkle.observability.wrap import patch_method

# 每个测试使用 *local* 类（无共享的模块级状态）以避免
# monkey-patch 的跨测试污染。


def test_patch_wraps_and_calls_original():
    class Dummy:
        async def method(self, x):
            return ("orig", x)

    calls = []

    def factory(orig):
        async def wrapped(self, x):
            calls.append(("wrapped", x))
            r = await orig(self, x)
            calls.append(("after", r))
            return r

        return wrapped

    assert patch_method(Dummy, "method", factory) is True

    async def run():
        return await Dummy().method(5)

    out = asyncio.run(run())
    assert out == ("orig", 5)
    assert calls == [("wrapped", 5), ("after", ("orig", 5))]


def test_patch_is_idempotent():
    class Dummy:
        async def method(self, x):
            return x

    def factory(orig):
        async def wrapped(self, x):
            return await orig(self, x)

        return wrapped

    assert patch_method(Dummy, "method", factory) is True
    assert patch_method(Dummy, "method", factory) is False  # 已包装过


def test_patch_failsoft_missing_method():
    class Dummy:
        pass

    assert patch_method(Dummy, "nope", lambda o: o) is False


def test_patch_failsoft_factory_error_leaves_original_intact():
    class Dummy:
        async def m(self):
            return 1

    def bad_factory(orig):
        raise RuntimeError("boom")

    assert patch_method(Dummy, "m", bad_factory) is False

    async def run():
        return await Dummy().m()

    assert asyncio.run(run()) == 1


from twinkle.observability.context import (
    current_llm_counter,
    current_request_context,
    increment_llm_counter,
    reset_llm_counter,
    set_request_context,
)


def test_request_context_set_and_reset():
    assert current_request_context() is None
    tok = set_request_context(request_id="r1", session_id="s1", agent_name="AgentLoop")
    ctx = current_request_context()
    assert ctx is not None
    assert ctx.request_id == "r1"
    assert ctx.session_id == "s1"
    assert ctx.agent_name == "AgentLoop"
    tok.reset()
    assert current_request_context() is None


def test_llm_counter_reset_and_increment():
    tok = reset_llm_counter()
    assert current_llm_counter() == 0
    increment_llm_counter()
    increment_llm_counter()
    assert current_llm_counter() == 2
    tok.reset()


from twinkle.observability.config import load_config

_OBS_KEYS = [
    "OTEL_ENABLED", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME",
]


def test_config_defaults(monkeypatch):
    for k in _OBS_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.enabled is False
    assert cfg.traces_exporter == "none"
    assert cfg.metrics_exporter == "none"
    assert cfg.protocol == "grpc"
    assert cfg.endpoint == ""
    assert cfg.headers == {}
    assert cfg.service_name == "twinkle-agentserver"


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "twinkle-agentserver")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "k1=v1, k2=v2")
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.traces_exporter == "otlp"
    assert cfg.metrics_exporter == "otlp"
    assert cfg.protocol == "grpc"
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.headers == {"k1": "v1", "k2": "v2"}


def test_tracer_exporter_collects_spans(tracer_exporter):
    tracer, exp = tracer_exporter
    with tracer.start_as_current_span("smoke") as span:
        span.set_attribute("k", "v")
    assert len(exp.spans) == 1
    assert exp.spans[0].name == "smoke"
    assert exp.spans[0].attributes["k"] == "v"


from twinkle.observability.metrics import Metrics


def _metric_names(reader):
    data = reader.get_metrics_data()
    names = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                names.append(m.name)
    return names


def test_metrics_record_token_usage(meter_metricreader):
    meter, reader = meter_metricreader
    m = Metrics(meter)
    m.record_token_usage(
        {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}, "gpt-4o-mini"
    )
    reader.force_flush()
    assert A.METRIC_TOKEN_USAGE in _metric_names(reader)


def test_metrics_record_tool_call(meter_metricreader):
    meter, reader = meter_metricreader
    m = Metrics(meter)
    m.record_tool_call("web_fetch", error=False, duration_s=0.12)
    reader.force_flush()
    names = _metric_names(reader)
    assert A.METRIC_TOOL_COUNT in names
    assert A.METRIC_TOOL_DURATION in names


def test_metrics_failsoft_none_usage(meter_metricreader):
    meter, _ = meter_metricreader
    m = Metrics(meter)
    m.record_token_usage(None, "m")  # 不得抛异常
    m.record_tool_call(None, error=True, duration_s=0.0)


def test_metrics_none_meter_is_silent_noop(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="twinkle.observability.metrics"):
        m = Metrics(None)
    # meter 为 None 的守卫必须静默跳过 instrument 创建（不输出
    # fail-soft 回溯）—— 这是 traces 开启 + metrics 关闭的路径。
    assert "create_counter failed" not in caplog.text
    assert "create_histogram failed" not in caplog.text
    m.record_token_usage({"prompt_tokens": 1}, "m")
    m.record_tool_call("x", error=False, duration_s=0.1)
    m.record_llm_duration("m", 0.1)
    m.record_agent_duration("succeeded", 0.1)


from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.observability.instrumentors.llm import instrument_llm


class _Cfg:
    """instrumentor 测试的 Config 替身（始终捕获 input/output）。"""
    pass


class _FakeLLMBase:
    def __init__(self):
        self._model = "fake-model"

    async def stream(self, messages, tools):
        yield TextDelta("hello")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "hello", "tool_calls": None},
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        )


def test_instrument_llm_emits_gen_ai_chat_span(tracer_exporter, meter_metricreader):
    class _FakeLLM(_FakeLLMBase):
        pass

    tracer, exp = tracer_exporter
    meter, reader = meter_metricreader
    metrics = Metrics(meter)
    assert instrument_llm(tracer, metrics, _Cfg(), llm_cls=_FakeLLM) is True

    async def run():
        return [e async for e in _FakeLLM().stream(messages=[], tools=[])]

    events = asyncio.run(run())
    assert [type(e).__name__ for e in events] == ["TextDelta", "Finish"]

    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "gen_ai.chat"
    attrs = span.attributes
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.request.model"] == "fake-model"
    assert attrs["gen_ai.response.finish_reason"] == "stop"
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 7
    assert isinstance(attrs["gen_ai.streaming.first_token_ms"], int)
    assert "gen_ai.input.messages" in attrs  # 现在始终捕获
    assert "gen_ai.output.messages" in attrs


def test_instrument_llm_handles_pydantic_completion_usage(tracer_exporter, meter_metricreader):
    """回归测试：真实 openai SDK 把 Finish.usage 作为 CompletionUsage pydantic 对象
    而非 dict 产出。通过 .get() 读 token 会抛 AttributeError 并搞崩整个 agent invoke
    （只有 2 个 span、status=ERROR、没有 usage/metrics）。必须同时支持 dict（fake/测试）
    和 pydantic 对象。"""
    from openai.types import CompletionUsage

    class _FakeLLM:
        def __init__(self):
            self._model = "fake-model"

        async def stream(self, messages, tools):
            yield TextDelta("hello")
            yield Finish(
                finish_reason="stop",
                assistant_message={"role": "assistant", "content": "hello", "tool_calls": None},
                usage=CompletionUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            )

    tracer, exp = tracer_exporter
    meter, reader = meter_metricreader
    metrics = Metrics(meter)
    instrument_llm(tracer, metrics, _Cfg(), llm_cls=_FakeLLM)

    async def run():
        return [e async for e in _FakeLLM().stream(messages=[], tools=[])]

    events = asyncio.run(run())  # 以前会抛 AttributeError
    assert [type(e).__name__ for e in events] == ["TextDelta", "Finish"]

    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "gen_ai.chat"
    assert span.status.status_code.name != "ERROR"  # 不得标记为失败
    attrs = span.attributes
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 7

    # metrics 也必须从 pydantic 对象记录 token 用量
    reader.force_flush()
    assert A.METRIC_TOKEN_USAGE in _metric_names(reader)


def test_instrument_llm_captures_message_content(tracer_exporter, meter_metricreader):
    class _FakeLLM(_FakeLLMBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_llm(tracer, metrics, _Cfg(), llm_cls=_FakeLLM)

    async def run():
        return [e async for e in _FakeLLM().stream(messages=[{"role": "user", "content": "hi"}], tools=[])]

    asyncio.run(run())
    assert len(exp.spans) == 1
    attrs = exp.spans[0].attributes
    assert "gen_ai.input.messages" in attrs
    assert "hi" in attrs["gen_ai.input.messages"]  # 实际内容已捕获
    assert "gen_ai.output.messages" in attrs


from twinkle.observability.instrumentors.tool import instrument_tool


class _FakeToolManagerBase:
    async def execute(self, name, args):
        if name == "boom":
            return "[tool error] ValueError: bad arg"
        return "ok-result"


def test_instrument_tool_emits_gen_ai_tool_span(tracer_exporter, meter_metricreader):
    class _FakeToolManager(_FakeToolManagerBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    assert instrument_tool(tracer, metrics, _Cfg(), tool_cls=_FakeToolManager) is True

    async def run():
        return await _FakeToolManager().execute("web_fetch", {"url": "x"})

    out = asyncio.run(run())
    assert out == "ok-result"
    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "gen_ai.tool"
    assert span.attributes["gen_ai.tool.name"] == "web_fetch"
    assert span.attributes["gen_ai.tool.error"] is False
    assert "gen_ai.tool.arguments" in span.attributes  # 现在始终捕获
    assert "gen_ai.tool.result" in span.attributes


def test_instrument_tool_marks_error_on_tool_error_prefix(tracer_exporter, meter_metricreader):
    class _FakeToolManager(_FakeToolManagerBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_tool(tracer, metrics, _Cfg(), tool_cls=_FakeToolManager)

    async def run():
        return await _FakeToolManager().execute("boom", {})

    out = asyncio.run(run())
    assert out.startswith("[tool error]")
    span = exp.spans[0]
    assert span.attributes["gen_ai.tool.name"] == "boom"
    assert span.attributes["gen_ai.tool.error"] is True


def test_instrument_tool_captures_args_result(tracer_exporter, meter_metricreader):
    class _FakeToolManager(_FakeToolManagerBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_tool(tracer, metrics, _Cfg(), tool_cls=_FakeToolManager)

    async def run():
        return await _FakeToolManager().execute("web_fetch", {"url": "x"})

    asyncio.run(run())
    attrs = exp.spans[0].attributes
    assert "gen_ai.tool.arguments" in attrs
    assert "gen_ai.tool.result" in attrs


from twinkle.observability.instrumentors.llm import _trunc


def test_trunc_default_is_full_no_truncation(monkeypatch):
    """默认（未设置 TWINKLE_OBS_ATTR_LIMIT）捕获完整内容 —— trace 的
    input/output 无需配置即可完整可见（以前截断到 4096）。"""
    monkeypatch.delenv("TWINKLE_OBS_ATTR_LIMIT", raising=False)
    big = "x" * 50000
    assert _trunc(big) == big


def test_trunc_explicit_zero_is_full(monkeypatch):
    monkeypatch.setenv("TWINKLE_OBS_ATTR_LIMIT", "0")
    big = "y" * 50000
    assert _trunc(big) == big


def test_trunc_custom_limit_caps(monkeypatch):
    monkeypatch.setenv("TWINKLE_OBS_ATTR_LIMIT", "4096")
    out = _trunc("z" * 5000)
    assert out.endswith("...")
    assert len(out) == 4096 + 3  # 上限 + 省略号后缀


from twinkle.observability.instrumentors.agent import instrument_agent


class _FakeEnvelope:
    def __init__(self, request_id="req-1", session_id="sess-1", params=None):
        self.request_id = request_id
        self.session_id = session_id
        self.params = params or {}


class _FakeAgentBase:
    async def run(self, request):
        yield "frame-1"
        yield "frame-2"


class _BoomAgentBase:
    async def run(self, request):
        yield "frame-1"
        raise RuntimeError("loop failed")


def test_instrument_agent_emits_invoke_span(tracer_exporter, meter_metricreader):
    class _FakeAgent(_FakeAgentBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    assert instrument_agent(tracer, metrics, _Cfg(), agent_cls=_FakeAgent) is True

    async def run():
        return [f async for f in _FakeAgent().run(_FakeEnvelope("req-1", "sess-1"))]

    frames = asyncio.run(run())
    assert frames == ["frame-1", "frame-2"]
    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "twinkle.agent.invoke"
    assert span.parent is None  # 根 span
    assert span.attributes["twinkle.request.id"] == "req-1"
    assert span.attributes["twinkle.session.id"] == "sess-1"
    assert span.attributes["twinkle.agent.iterations"] == 0  # 这个 fake 中没有 llm 调用
    assert span.attributes["twinkle.agent.status"] == "succeeded"


def test_instrument_agent_records_error_status_and_reraises(tracer_exporter, meter_metricreader):
    class _BoomAgent(_BoomAgentBase):
        pass

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_agent(tracer, metrics, _Cfg(), agent_cls=_BoomAgent)

    async def run():
        out = []
        try:
            async for f in _BoomAgent().run(_FakeEnvelope()):
                out.append(f)
        except RuntimeError:
            return out
        return out

    out = asyncio.run(run())
    assert out == ["frame-1"]
    span = exp.spans[0]
    assert span.attributes["twinkle.agent.status"] == "failed"
    assert span.status.status_code.name == "ERROR"


class _E2AFrame:
    """用于 agent-instrumentor 状态测试的最小 duck-typed E2AResponse。"""

    def __init__(self, response_kind: str, status: str):
        self.response_kind = response_kind
        self.status = status


def test_instrument_agent_marks_failed_on_e2a_error_frame(tracer_exporter, meter_metricreader):
    # MAX_STEPS -> agent loop 产出 e2a.error 并正常返回（无异常）；
    # span 必须反映真实结果（failed），不能被误标为 "succeeded"。
    class _FakeAgent:
        async def run(self, request):
            yield _E2AFrame("e2a.error", "failed")

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_agent(tracer, metrics, _Cfg(), agent_cls=_FakeAgent)

    async def run():
        return [f async for f in _FakeAgent().run(_FakeEnvelope())]

    frames = asyncio.run(run())
    assert len(frames) == 1
    span = exp.spans[0]
    assert span.attributes["twinkle.agent.status"] == "failed"
    assert span.status.status_code.name == "ERROR"


def test_instrument_agent_marks_succeeded_on_e2a_complete_frame(tracer_exporter, meter_metricreader):
    class _FakeAgent:
        async def run(self, request):
            yield _E2AFrame("e2a.complete", "succeeded")

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_agent(tracer, metrics, _Cfg(), agent_cls=_FakeAgent)

    async def run():
        return [f async for f in _FakeAgent().run(_FakeEnvelope())]

    asyncio.run(run())
    span = exp.spans[0]
    assert span.attributes["twinkle.agent.status"] == "succeeded"


from twinkle.observability.provider import init_providers


def test_init_providers_none_when_exporter_none(monkeypatch):
    for k in _OBS_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    tracer, meter = init_providers(cfg)
    assert tracer is None
    assert meter is None


def test_init_providers_console_returns_tracer_and_meter(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "console")
    cfg = load_config()
    tracer, meter = init_providers(cfg)
    assert tracer is not None
    assert meter is not None
    # （Tracer 功能由 test_tracer_exporter_collects_spans 覆盖；
    # 这里不开 span，以避免 console BatchSpanProcessor 在解释器关闭时
    # 向已关闭的 stderr 导出。）


from twinkle.observability import setup
from twinkle.observability.instrumentors import apply_instrumentors


def test_setup_noop_when_disabled(monkeypatch):
    for k in _OBS_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert setup() is False
    assert setup() is False  # 仍然是 no-op，不抛异常，_APPLIED 保持 False


# --- 端到端：完整 trace 树（agent.invoke -> gen_ai.chat + gen_ai.tool）---
# 只有一个测试用到这些模块级 fake，所以 patch 它们是隔离的。

class _IntegLLM:
    def __init__(self):
        self._model = "integ-model"

    async def stream(self, messages, tools):
        yield TextDelta("ans")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "ans", "tool_calls": None},
            usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        )


class _IntegTool:
    async def execute(self, name, args):
        return "tool-out"


class _IntegAgent:
    def __init__(self, llm, tools):
        self._llm = llm
        self._tools = tools

    async def run(self, request):
        async for ev in self._llm.stream([], []):
            yield ev
        await self._tools.execute("web_fetch", {"url": "x"})


class _IntegEnvelope:
    def __init__(self):
        self.request_id = "req-x"
        self.session_id = "sess-x"
        self.params = {}


def _fresh_fake_compression_mod():
    """新鲜的 module-like 对象，使 apply_instrumentors 的 compression 条目 patch 一个
    隔离的目标。真实 compression 模块是跨测试共享的 singleton；patch_method 的幂等守卫
    在第 2 次 patch 时返回 False，这会让后续测试中 results["compression"] 为 False。
    """
    mod = types.ModuleType("fake_compression_obs")

    async def do_compress(msgs, llm, *, keep_recent_pairs, summary_system_prompt):
        return list(msgs)

    mod.do_compress = do_compress
    mod.estimate_tokens = lambda msgs: 0
    return mod


class _FakeEvoNoop:
    """用于 apply_instrumentors 隔离的 noop orchestrator（每个测试用全新类
    避免共享的真实 orchestrator 上的幂等守卫）。"""
    async def evolve(self, skill_name, conversation_messages, *a, **k):
        return None


def test_full_trace_tree(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    cfg = _Cfg()
    results = apply_instrumentors(
        tracer, metrics, cfg,
        agent_cls=_IntegAgent, llm_cls=_IntegLLM, tool_cls=_IntegTool,
        compression_mod=_fresh_fake_compression_mod(), orchestrator_cls=_FakeEvoNoop,
    )
    assert results["agent"] is True
    assert results["llm"] is True
    assert results["tool"] is True
    assert results["compression"] is True
    assert results["evolution"] is True

    agent = _IntegAgent(_IntegLLM(), _IntegTool())

    async def run():
        return [f async for f in agent.run(_IntegEnvelope())]

    asyncio.run(run())

    names = [s.name for s in exp.spans]
    assert "twinkle.agent.invoke" in names
    assert "gen_ai.chat" in names
    assert "gen_ai.tool" in names

    roots = [s for s in exp.spans if s.parent is None]
    assert len(roots) == 1
    agent_span = roots[0]
    assert agent_span.name == "twinkle.agent.invoke"
    assert agent_span.attributes["twinkle.request.id"] == "req-x"
    assert agent_span.attributes["twinkle.session.id"] == "sess-x"
    assert agent_span.attributes["twinkle.agent.iterations"] == 1  # 一次 llm 调用
    assert agent_span.attributes["twinkle.agent.status"] == "succeeded"

    chat_span = next(s for s in exp.spans if s.name == "gen_ai.chat")
    tool_span = next(s for s in exp.spans if s.name == "gen_ai.tool")
    assert chat_span.parent is not None
    assert tool_span.parent is not None
    # 两个子 span 都是 agent span 的直接子节点
    assert chat_span.parent.span_id == agent_span.context.span_id
    assert tool_span.parent.span_id == agent_span.context.span_id


# --- subagent：嵌套的 invoke span 必须挂在 tool span 下（而非外层 invoke）---
# 一个内部运行另一个已 instrument 的 agent loop 的 tool（模拟 spawn_subagent）。
# 由于 tool 上用 start_as_current_span，嵌套的 twinkle.agent.invoke span 的 parent
# 必须是 gen_ai.tool span（tool 执行期间的 current span）。
# 修复前（用 start_span 而非 current）嵌套 invoke 会挂到外层 agent invoke 下。

class _SubLLM:
    """仅用于本测试的新鲜 LLM 类（不与 test_full_trace_tree 共享，因此
    apply_instrumentors 的幂等守卫不会在一个已 patch 的类上触发）。"""

    def __init__(self):
        self._model = "sub-model"

    async def stream(self, messages, tools):
        yield TextDelta("ans")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "ans", "tool_calls": None},
            usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        )


class _RecurAgent:
    """从其 llm 流式产出；若有 tools 则调用 spawn_subagent。同时用作外层 agent
    和嵌套子 agent（同一个已 instrument 的类）。"""

    def __init__(self, llm, tools=None):
        self._llm = llm
        self._tools = tools

    async def run(self, request):
        async for ev in self._llm.stream([], []):
            yield ev
        if self._tools is not None:
            await self._tools.execute("spawn_subagent", {})


class _SpawnTool:
    """模拟 spawn_subagent：execute 在 child task 中运行嵌套 agent 的 run。
    asyncio.create_task 会复制 OTel context，因此嵌套 invoke span 的 parent
    = create_task 时的 current span。"""

    def __init__(self, child_agent):
        self._child = child_agent

    async def execute(self, name, args):
        if name != "spawn_subagent":
            return "ok"

        async def _drain():
            async for _ in self._child.run(_IntegEnvelope()):
                pass

        await asyncio.create_task(_drain())
        return "child-result"


def test_subagent_invoke_span_nests_under_tool_span(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    cfg = _Cfg()

    class _FakeEvoLocal:
        async def evolve(self, skill_name, conversation_messages, *a, **k):
            return None

    results = apply_instrumentors(
        tracer, metrics, cfg,
        agent_cls=_RecurAgent, llm_cls=_SubLLM, tool_cls=_SpawnTool,
        compression_mod=_fresh_fake_compression_mod(), orchestrator_cls=_FakeEvoLocal,
    )
    assert results["agent"] is True
    assert results["llm"] is True
    assert results["tool"] is True
    assert results["compression"] is True
    assert results["evolution"] is True

    child_agent = _RecurAgent(_SubLLM(), tools=None)   # 嵌套：仅流式产出
    tool = _SpawnTool(child_agent)
    outer = _RecurAgent(_SubLLM(), tools=tool)         # 外层：流式产出 + 调用 tool

    async def run():
        return [f async for f in outer.run(_IntegEnvelope())]

    asyncio.run(run())

    names = [s.name for s in exp.spans]
    # 外层 invoke + 外层 chat + tool + 嵌套 invoke + 嵌套 chat
    assert names.count("twinkle.agent.invoke") == 2
    assert names.count("gen_ai.chat") == 2
    assert names.count("gen_ai.tool") == 1

    invokes = [s for s in exp.spans if s.name == "twinkle.agent.invoke"]
    roots = [s for s in invokes if s.parent is None]
    nested = [s for s in invokes if s.parent is not None]
    assert len(roots) == 1 and len(nested) == 1
    outer_invoke = roots[0]
    nested_invoke = nested[0]

    tool_span = next(s for s in exp.spans if s.name == "gen_ai.tool")
    # tool span 的 parent 是外层 invoke（tool 运行时的 current span）
    assert tool_span.parent.span_id == outer_invoke.context.span_id
    # 嵌套 invoke 的 parent 必须是 tool span（即修复点）—— 不是外层 invoke。
    # 修复前这里断言的是 outer_invoke.context.span_id。
    assert nested_invoke.parent.span_id == tool_span.context.span_id
