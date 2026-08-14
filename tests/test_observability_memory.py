"""MemoryFlushHook 可观测 instrumentor 测试。

对齐 tests/test_observability_compression_evolution.py 结构：fake 类（不 patch 真
MemoryFlushHook，免跨测试污染）+ 文件内 tracer_exporter fixture（module-isolated）。
"""
import asyncio

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)
from opentelemetry.trace import StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.memory import instrument_memory_flush
from twinkle.observability.metrics import Metrics


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


class _Cfg:
    pass


class _FakeFlushHook:
    """_flush 返回预设 (new_writes, errors)。"""
    def __init__(self, result=(2, 0)):
        self._result = result

    async def _flush(self, middle):
        return self._result


class _RaisingFlushHook:
    async def _flush(self, middle):
        raise RuntimeError("flush outage")


def test_flush_instrumentor_opens_span_with_attrs(tracer_exporter):
    tracer, exp = tracer_exporter
    assert instrument_memory_flush(
        tracer, Metrics(None), _Cfg(), hook_cls=_FakeFlushHook) is True

    hook = _FakeFlushHook(result=(3, 1))
    result = asyncio.run(hook._flush([{"role": "user", "content": "m"}]))
    assert result == (3, 1)

    spans = [s for s in exp.spans if s.name == A.SPAN_MEMORY_FLUSH]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs[A.TWINKLE_MEMORY_FLUSH_NEW_WRITES] == 3
    assert attrs[A.TWINKLE_MEMORY_FLUSH_ERRORS] == 1


def test_flush_instrumentor_error_sets_status_and_reraises(tracer_exporter):
    tracer, exp = tracer_exporter
    assert instrument_memory_flush(
        tracer, Metrics(None), _Cfg(), hook_cls=_RaisingFlushHook) is True

    hook = _RaisingFlushHook()
    with pytest.raises(RuntimeError):
        asyncio.run(hook._flush([{"role": "user", "content": "m"}]))

    spans = [s for s in exp.spans if s.name == A.SPAN_MEMORY_FLUSH]
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    # >=1 而非 ==1：手动 record + SDK 退出自动记 escaped，钉死计数耦合 SDK 版本。
    exc_events = [e for e in spans[0].events if e.name == "exception"]
    assert len(exc_events) >= 1


def test_flush_instrumentor_idempotent(tracer_exporter):
    tracer, _ = tracer_exporter

    class _Fresh:
        async def _flush(self, middle):
            return 0, 0

    assert instrument_memory_flush(
        tracer, Metrics(None), _Cfg(), hook_cls=_Fresh) is True
    assert instrument_memory_flush(
        tracer, Metrics(None), _Cfg(), hook_cls=_Fresh) is False
