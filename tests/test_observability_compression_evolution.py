import asyncio

import pytest

# Skip whole file if [obs] not installed — keeps suite green without opentelemetry.
pytest.importorskip("opentelemetry.sdk")

import types

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.compression import instrument_compression
from twinkle.observability.instrumentors.evolution import instrument_evolution
from twinkle.observability.instrumentors.llm import instrument_llm
from twinkle.observability.instrumentors import apply_instrumentors
from twinkle.observability.metrics import Metrics


# --- fixtures (mirrors tests/test_observability.py, module-isolated) ---

class CollectingSpanExporter(SpanExporter):
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


class _Cfg:
    pass


# --- fakes ---

class _SummaryLLM:
    """Yields a summary TextDelta + Finish. Reused as the patched llm class
    so instrument_llm emits a nested gen_ai.chat span under the compression span."""
    def __init__(self):
        self._model = "summary-model"

    async def stream(self, messages, tools):
        yield TextDelta("历史摘要")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "历史摘要", "tool_calls": None},
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        )


class _RaisingLLM:
    async def stream(self, messages, tools):
        raise RuntimeError("summary outage")
        yield  # makes this an async generator


def _big_msgs():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    msgs += [{"role": "assistant", "content": f"a{i} " + "y" * 30} for i in range(20)]
    return msgs


def _tiny_msgs():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


# --- compression: real-module integration (noop + fire + degrade, one patch) ---

def test_compression_real_path_noop_fire_degrade(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    # Patch the REAL compression module (compression_mod=None -> lazy import).
    assert instrument_compression(tracer, metrics, _Cfg()) is True
    # Patch the llm class so the summary call emits a nested gen_ai.chat.
    instrument_llm(tracer, metrics, _Cfg(), llm_cls=_SummaryLLM)

    from twinkle.agentserver.compression import compress_messages, estimate_tokens

    # Scenario A: under threshold -> should_compress False -> no do_compress call -> no span.
    n0 = len(exp.spans)
    out = asyncio.run(compress_messages(
        _tiny_msgs(), _SummaryLLM(), token_threshold=10 ** 9,
        keep_recent_pairs=6, summary_system_prompt="p"))
    assert out == _tiny_msgs()
    assert len(exp.spans) == n0  # no new span

    # Scenario B: over threshold -> span + nested gen_ai.chat child.
    n1 = len(exp.spans)
    out = asyncio.run(compress_messages(
        _big_msgs(), _SummaryLLM(), token_threshold=10,
        keep_recent_pairs=3, summary_system_prompt="p"))
    assert estimate_tokens(out) < estimate_tokens(_big_msgs())
    comp_spans = [s for s in exp.spans[n1:] if s.name == A.SPAN_COMPRESSION]
    assert len(comp_spans) == 1
    cs = comp_spans[0]
    attrs = cs.attributes
    assert attrs[A.TWINKLE_COMPRESSION_TOKENS_BEFORE] > attrs[A.TWINKLE_COMPRESSION_TOKENS_AFTER]
    assert attrs[A.TWINKLE_COMPRESSION_COMPRESSED] is True
    assert attrs[A.TWINKLE_COMPRESSION_HAS_SUMMARY] is True
    assert attrs[A.TWINKLE_COMPRESSION_STRATEGY] == "inline_summary"
    # nested gen_ai.chat child parents to the compression span
    chat_spans = [s for s in exp.spans[n1:] if s.name == A.SPAN_GEN_AI_CHAT]
    assert len(chat_spans) == 1
    assert chat_spans[0].parent is not None
    assert chat_spans[0].parent.span_id == cs.context.span_id

    # Scenario C: summary raises -> degrade -> has_summary False, compressed True.
    n2 = len(exp.spans)
    out = asyncio.run(compress_messages(
        _big_msgs(), _RaisingLLM(), token_threshold=10,
        keep_recent_pairs=3, summary_system_prompt="p"))
    comp_spans = [s for s in exp.spans[n2:] if s.name == A.SPAN_COMPRESSION]
    assert len(comp_spans) == 1
    assert comp_spans[0].attributes[A.TWINKLE_COMPRESSION_HAS_SUMMARY] is False
    assert comp_spans[0].attributes[A.TWINKLE_COMPRESSION_COMPRESSED] is True


def _fake_compression_mod():
    """Fresh module-like object (isolated, not idempotent-blocked across tests)."""
    mod = types.ModuleType("fake_compression")

    async def do_compress(msgs, llm, *, keep_recent_pairs, summary_system_prompt):
        return list(msgs)

    mod.do_compress = do_compress
    mod.estimate_tokens = lambda msgs: 0
    return mod


def test_compression_idempotent(tracer_exporter):
    tracer, _ = tracer_exporter
    fake = _fake_compression_mod()
    assert instrument_compression(tracer, Metrics(None), _Cfg(), compression_mod=fake) is True
    assert instrument_compression(tracer, Metrics(None), _Cfg(), compression_mod=fake) is False


# --- evolution: patch OnlineEvolutionOrchestrator.evolve ---

from twinkle.agentserver.evolution.orchestrator import EvolutionResult


class _FakeEvoOrchestrator:
    """Minimal orchestrator: evolve returns a staged EvolutionResult."""
    async def evolve(self, skill_name, conversation_messages, *args, **kwargs):
        return EvolutionResult(status="generated", skill_name=skill_name,
                               message="2 records staged")


class _BoomEvoOrchestrator:
    async def evolve(self, skill_name, conversation_messages, *args, **kwargs):
        raise RuntimeError("evolve boom")


def test_evolution_span_with_status(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    assert instrument_evolution(tracer, metrics, _Cfg(),
                               orchestrator_cls=_FakeEvoOrchestrator) is True

    async def run():
        return await _FakeEvoOrchestrator().evolve("my-skill", [])

    result = asyncio.run(run())
    assert result.status == "generated"
    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == A.SPAN_SKILL_EVOLUTION
    assert span.attributes[A.TWINKLE_SKILL_NAME] == "my-skill"
    assert span.attributes[A.TWINKLE_EVOLUTION_STATUS] == "generated"
    assert span.attributes[A.TWINKLE_EVOLUTION_MESSAGE] == "2 records staged"
    assert span.status.status_code.name != "ERROR"


def test_evolution_error_marks_span_and_reraises(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_evolution(tracer, metrics, _Cfg(), orchestrator_cls=_BoomEvoOrchestrator)

    async def run():
        try:
            await _BoomEvoOrchestrator().evolve("boom-skill", [])
            return "no-raise"
        except RuntimeError:
            return "reraised"

    out = asyncio.run(run())
    assert out == "reraised"
    assert len(exp.spans) == 1
    span = exp.spans[0]
    assert span.name == A.SPAN_SKILL_EVOLUTION
    assert span.attributes[A.TWINKLE_SKILL_NAME] == "boom-skill"
    assert span.status.status_code.name == "ERROR"


def test_evolution_idempotent(tracer_exporter):
    tracer, _ = tracer_exporter

    class _FreshEvoOrchestrator:
        """Fresh class per test so the first patch isn't blocked by a prior
        test's wrapper marker (mirrors compression's fresh _fake_compression_mod)."""
        async def evolve(self, skill_name, conversation_messages, *args, **kwargs):
            return EvolutionResult(status="generated", skill_name=skill_name,
                                   message="ok")

    assert instrument_evolution(tracer, Metrics(None), _Cfg(),
                                orchestrator_cls=_FreshEvoOrchestrator) is True
    assert instrument_evolution(tracer, Metrics(None), _Cfg(),
                                orchestrator_cls=_FreshEvoOrchestrator) is False


class _NoopAgent:
    async def run(self, request):
        yield "f"


class _NoopLLM:
    def __init__(self):
        self._model = "noop"

    async def stream(self, messages, tools):
        return
        yield  # async generator


class _NoopTool:
    async def execute(self, name, args):
        return "ok"


def test_apply_instrumentors_registers_compression_and_evolution(
        tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    fake_comp = _fake_compression_mod()

    class _FakeEvo:
        async def evolve(self, skill_name, conversation_messages, *a, **k):
            return None

    results = apply_instrumentors(
        tracer, metrics, _Cfg(),
        agent_cls=_NoopAgent, llm_cls=_NoopLLM, tool_cls=_NoopTool,
        compression_mod=fake_comp, orchestrator_cls=_FakeEvo,
    )
    assert results["agent"] is True
    assert results["llm"] is True
    assert results["tool"] is True
    assert results["compression"] is True
    assert results["evolution"] is True
