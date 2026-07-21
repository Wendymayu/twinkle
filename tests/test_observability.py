import pytest

# Skip the whole file gracefully if [obs] isn't installed — keeps the
# existing test suite green without opentelemetry.
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
    """In-memory SpanExporter; appended spans available via .spans."""

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


import asyncio

from twinkle.observability.wrap import patch_method

# Each test uses a *local* class (no shared module-level state) to avoid
# monkey-patch cross-test pollution.


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
    assert patch_method(Dummy, "method", factory) is False  # already wrapped


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
    m.record_token_usage(None, "m")  # must not raise
    m.record_tool_call(None, error=True, duration_s=0.0)


def test_metrics_none_meter_is_silent_noop(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="twinkle.observability.metrics"):
        m = Metrics(None)
    # The meter-None guard must skip instrument creation silently (no
    # fail-soft tracebacks) — this is the traces-on + metrics-off path.
    assert "create_counter failed" not in caplog.text
    assert "create_histogram failed" not in caplog.text
    m.record_token_usage({"prompt_tokens": 1}, "m")
    m.record_tool_call("x", error=False, duration_s=0.1)
    m.record_llm_duration("m", 0.1)
    m.record_agent_duration("succeeded", 0.1)


from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.observability.instrumentors.llm import instrument_llm


class _Cfg:
    """Config stand-in for instrumentor tests (input/output always captured)."""
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
    assert "gen_ai.input.messages" in attrs  # always captured now
    assert "gen_ai.output.messages" in attrs


def test_instrument_llm_handles_pydantic_completion_usage(tracer_exporter, meter_metricreader):
    """Regression: the real openai SDK yields Finish.usage as a
    CompletionUsage pydantic object, not a dict. Reading tokens via .get()
    raised AttributeError and broke the whole agent invoke (only 2 spans,
    status=ERROR, no usage/metrics). Must support both dict (fakes/tests)
    and pydantic objects."""
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

    events = asyncio.run(run())  # previously raised AttributeError
    assert [type(e).__name__ for e in events] == ["TextDelta", "Finish"]

    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "gen_ai.chat"
    assert span.status.status_code.name != "ERROR"  # must not be marked failed
    attrs = span.attributes
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 7

    # metrics must record token usage from the pydantic object too
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
    assert "hi" in attrs["gen_ai.input.messages"]  # actual content captured
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
    assert "gen_ai.tool.arguments" in span.attributes  # always captured now
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


from twinkle.observability.instrumentors.agent import instrument_agent


class _FakeEnvelope:
    def __init__(self, request_id="req-1", session_id="sess-1", params=None):
        self.request_id = request_id
        self.session_id = session_id
        self.params = params or {}


class _FakeAgentBase:
    async def run_stream(self, envelope):
        yield "frame-1"
        yield "frame-2"


class _BoomAgentBase:
    async def run_stream(self, envelope):
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
        return [f async for f in _FakeAgent().run_stream(_FakeEnvelope("req-1", "sess-1"))]

    frames = asyncio.run(run())
    assert frames == ["frame-1", "frame-2"]
    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == "twinkle.agent.invoke"
    assert span.parent is None  # root
    assert span.attributes["twinkle.request.id"] == "req-1"
    assert span.attributes["twinkle.session.id"] == "sess-1"
    assert span.attributes["twinkle.agent.iterations"] == 0  # no llm call in this fake
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
            async for f in _BoomAgent().run_stream(_FakeEnvelope()):
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
    """Minimal duck-typed E2AResponse for agent-instrumentor status tests."""

    def __init__(self, response_kind: str, status: str):
        self.response_kind = response_kind
        self.status = status


def test_instrument_agent_marks_failed_on_e2a_error_frame(tracer_exporter, meter_metricreader):
    # MAX_STEPS -> agent loop yields e2a.error and returns normally (no exception);
    # the span must reflect the real outcome (failed), not be mislabeled "succeeded".
    class _FakeAgent:
        async def run_stream(self, envelope):
            yield _E2AFrame("e2a.error", "failed")

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_agent(tracer, metrics, _Cfg(), agent_cls=_FakeAgent)

    async def run():
        return [f async for f in _FakeAgent().run_stream(_FakeEnvelope())]

    frames = asyncio.run(run())
    assert len(frames) == 1
    span = exp.spans[0]
    assert span.attributes["twinkle.agent.status"] == "failed"
    assert span.status.status_code.name == "ERROR"


def test_instrument_agent_marks_succeeded_on_e2a_complete_frame(tracer_exporter, meter_metricreader):
    class _FakeAgent:
        async def run_stream(self, envelope):
            yield _E2AFrame("e2a.complete", "succeeded")

    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_agent(tracer, metrics, _Cfg(), agent_cls=_FakeAgent)

    async def run():
        return [f async for f in _FakeAgent().run_stream(_FakeEnvelope())]

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
    # (Tracer functionality is covered by test_tracer_exporter_collects_spans;
    # we don't start a span here to avoid the console BatchSpanProcessor
    # exporting to closed stderr at interpreter shutdown.)


from twinkle.observability import setup
from twinkle.observability.instrumentors import apply_instrumentors


def test_setup_noop_when_disabled(monkeypatch):
    for k in _OBS_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert setup() is False
    assert setup() is False  # still no-op, no raise, _APPLIED stays False


# --- end-to-end: full trace tree (agent.invoke -> gen_ai.chat + gen_ai.tool) ---
# Only one test uses these module-level fakes, so patching them is isolated.

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

    async def run_stream(self, envelope):
        async for ev in self._llm.stream([], []):
            yield ev
        await self._tools.execute("web_fetch", {"url": "x"})


class _IntegEnvelope:
    def __init__(self):
        self.request_id = "req-x"
        self.session_id = "sess-x"
        self.params = {}


def test_full_trace_tree(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    cfg = _Cfg()
    results = apply_instrumentors(
        tracer, metrics, cfg,
        agent_cls=_IntegAgent, llm_cls=_IntegLLM, tool_cls=_IntegTool,
    )
    assert results == {"agent": True, "llm": True, "tool": True}

    agent = _IntegAgent(_IntegLLM(), _IntegTool())

    async def run():
        return [f async for f in agent.run_stream(_IntegEnvelope())]

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
    assert agent_span.attributes["twinkle.agent.iterations"] == 1  # one llm call
    assert agent_span.attributes["twinkle.agent.status"] == "succeeded"

    chat_span = next(s for s in exp.spans if s.name == "gen_ai.chat")
    tool_span = next(s for s in exp.spans if s.name == "gen_ai.tool")
    assert chat_span.parent is not None
    assert tool_span.parent is not None
    # both children are direct children of the agent span
    assert chat_span.parent.span_id == agent_span.context.span_id
    assert tool_span.parent.span_id == agent_span.context.span_id
