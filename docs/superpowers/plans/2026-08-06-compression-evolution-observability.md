# 上下文压缩 + Skill 进化可观测性埋点 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为上下文压缩与 skill 进化补 OTel span 级可观测性,使其在 trace 中与工具调用对等可见。

**Architecture:** 拆 `compress_messages` 成 `should_compress`(判定)+ `do_compress`(执行)+ 薄壳 `compress_messages`(委派);新增两个 instrumentor 以 `patch_method` monkey-patch `do_compress` 与 `OnlineEvolutionOrchestrator.evolve`,埋点逻辑全在 `twinkle/observability/instrumentors/`。压缩 wrapper 在 `do_compress` 外开 `start_as_current_span`,摘要 `gen_ai.chat` 自然嵌套;`do_compress` 只在 `should_compress` 为真时被调到,故无假阳性、无预测逻辑。evolution wrapper 裹 `evolve`,记 `status`/`skill_name`。不采集 metric(低频)。

**Tech Stack:** Python 3, opentelemetry-sdk (monkey-patch via `patch_method`), pytest (无 pytest-asyncio,用 `asyncio.run`)。

## Global Constraints

- 沿用现有 `patch_method`(`twinkle/observability/wrap.py`)幂等(`_twinkle_wrapped` 标记)、fail-soft idiom。
- 业务侧只动 `twinkle/agentserver/compression/__init__.py`(内部拆分,公开 API `compress_messages` 行为不变);两个 hook(`context_compression_hook.py` / `context_overflow_recovery_hook.py`)零改动。
- 测试无 `pytest-asyncio`——用 `asyncio.run(...)`(`tests/conftest.py` 约定)。
- OTel 测试用 `CollectingSpanExporter` + `SimpleSpanProcessor` + `TracerProvider` idiom(见 `tests/test_observability.py`)。
- Conventional commits(`feat(observability): ...`)。

## File Structure

| 文件 | 责任 | 任务 |
|---|---|---|
| `twinkle/agentserver/compression/__init__.py` | 拆 `should_compress` + `do_compress` + 薄壳 `compress_messages`(行为不变) | Task 1 |
| `twinkle/observability/attributes.py` | 新增 span 名 + 属性 key 常量 | Task 2 |
| `twinkle/observability/instrumentors/compression.py` | 新增 `instrument_compression`,patch `do_compress` → `twinkle.compression` span | Task 3 |
| `twinkle/observability/instrumentors/evolution.py` | 新增 `instrument_evolution`,patch `evolve` → `twinkle.skill.evolution` span | Task 4 |
| `twinkle/observability/instrumentors/__init__.py` | `apply_instrumentors` 注册两条 + 两个新参数 | Task 5 |
| `tests/test_context_compression.py` | Task 1 新增 `should_compress`/`do_compress` 契约测试 + 现有用例回归 | Task 1 |
| `tests/test_observability_compression_evolution.py` | 新增 instrumentor 测试(含内联 fixture) | Task 3/4/5 |
| `tests/test_observability.py` | Task 5 更新 `test_full_trace_tree` / `test_subagent_invoke_span_nests_under_tool_span` 的 results 断言(3→5 key) | Task 5 |

**说明:测试放新文件 `tests/test_observability_compression_evolution.py`(spec 已定),内联复制 `CollectingSpanExporter`/`tracer_exporter`/`meter_metricreader`/`_Cfg`(沿用 `tests/test_observability.py` 的同款 fixture,各测试模块独立、避免跨文件状态污染——与现有 `test_observability.py` 把 fixture 定义在模块级的做法一致)。**

---

### Task 1: 拆分 `compress_messages` 成 `should_compress` + `do_compress` + 薄壳

**Files:**
- Modify: `twinkle/agentserver/compression/__init__.py`(在现有 `compress_messages` 处重构)
- Test: `tests/test_context_compression.py`(加 import + 4 个新测试;现有测试作回归)

**Interfaces:**
- Produces:
  - `should_compress(msgs: list[dict], *, token_threshold: int, keep_recent_pairs: int) -> bool` — 两道闸(token + middle)
  - `async do_compress(msgs: list[dict], llm, *, keep_recent_pairs: int, summary_system_prompt: str) -> list[dict]` — 真正执行(含 LLM 摘要);`do_compress` 是 `compress_messages` 的同模块被调函数,经模块 globals 解析(这是 Task 3 patch 能到达的关键)
  - `async compress_messages(msgs, llm, *, token_threshold, keep_recent_pairs, summary_system_prompt) -> list[dict]` — 薄壳,签名与行为与重构前完全一致

- [ ] **Step 1: 写 `should_compress` / `do_compress` 的失败测试**

在 `tests/test_context_compression.py` 顶部 import 块追加 `should_compress`、`do_compress`:

```python
from twinkle.agentserver.compression import (
    _render_messages_text,
    _split_keep_tool_pairs,
    _summarize,
    compress_messages,
    do_compress,
    estimate_tokens,
    should_compress,
)
```

在文件末尾追加:

```python
# --- should_compress ---
def test_should_compress_false_under_threshold():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert should_compress(msgs, token_threshold=10_000, keep_recent_pairs=6) is False


def test_should_compress_false_when_tail_eats_all_middle():
    # tail_count (12) >= len(rest) (1) -> no middle even though over threshold
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert should_compress(msgs, token_threshold=1, keep_recent_pairs=6) is False


def test_should_compress_true_over_threshold_with_middle():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    assert should_compress(msgs, token_threshold=10, keep_recent_pairs=3) is True


# --- do_compress ---
def test_do_compress_summarizes_when_called_directly():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    out = asyncio.run(do_compress(
        msgs, FakeLLM(summary_text="摘要含 FACTKEY_5"),
        keep_recent_pairs=3, summary_system_prompt="p"))
    assert any("[prior context summary]" in m.get("content", "") for m in out)
    assert estimate_tokens(out) < estimate_tokens(msgs)


def test_do_compress_degrades_when_summary_fails():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    out = asyncio.run(do_compress(
        msgs, _RaisingLLM(), keep_recent_pairs=3, summary_system_prompt="p"))
    assert not any("[prior context summary]" in m.get("content", "") for m in out)
    assert estimate_tokens(out) < estimate_tokens(msgs)  # middle dropped
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_context_compression.py -v`
Expected: FAIL — `ImportError: cannot import name 'do_compress'` (and `should_compress`).

- [ ] **Step 3: 实现拆分**

在 `twinkle/agentserver/compression/__init__.py` 中,把现有 `async def compress_messages(...)` 整段替换为三个函数(保留文件其余部分:`estimate_tokens`、`_split_keep_tool_pairs`、`_render_messages_text`、`_summarize` 不动):

```python
def should_compress(msgs: list[dict], *, token_threshold: int,
                    keep_recent_pairs: int) -> bool:
    """两道闸:token 闸 + middle 闸。True 表示确实要压缩。

    抽成独立谓词供 compress_messages 委派、也供 instrumentor 判定是否产 span
    (避免 patch 执行函数时产生假阳性 span)。
    """
    if estimate_tokens(msgs) <= token_threshold:
        return False
    _head, middle, _tail = _split_keep_tool_pairs(msgs, tail_count=keep_recent_pairs * 2)
    return bool(middle)


async def do_compress(msgs: list[dict], llm: "LLMClient", *,
                      keep_recent_pairs: int,
                      summary_system_prompt: str) -> list[dict]:
    """真正执行压缩。假设 should_compress 已为 True(仍保留 `if not middle`
    兜底以防被直接调用)。含 _summarize 的 LLM 调用。返回新 list,不改输入。"""
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=keep_recent_pairs * 2)
    if not middle:
        return list(msgs)
    try:
        summary = await _summarize(llm, summary_system_prompt, _render_messages_text(middle))
    except Exception:
        # 摘要是优化非承重——降级为无摘要滑窗(head+tail,丢 middle)
        return head + tail
    summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
    return head + [summary_msg] + tail


async def compress_messages(
    msgs: list[dict],
    llm: "LLMClient",
    *,
    token_threshold: int,
    keep_recent_pairs: int,
    summary_system_prompt: str,
) -> list[dict]:
    """薄壳:判定 → 委派。公开 API,行为与重构前完全一致。

    No-op(copy) 当 should_compress 为 False。do_compress 经模块 globals 解析,
    被 instrumentor patch 后,本函数的调用会到达 wrapper(同模块解析,非 import 绑定)。
    """
    if not should_compress(msgs, token_threshold=token_threshold,
                           keep_recent_pairs=keep_recent_pairs):
        return list(msgs)
    return await do_compress(msgs, llm, keep_recent_pairs=keep_recent_pairs,
                             summary_system_prompt=summary_system_prompt)
```

- [ ] **Step 4: 运行测试确认通过(新测试 + 现有回归)**

Run: `python -m pytest tests/test_context_compression.py -v`
Expected: PASS — 全部(5 个新 + 原有 estimate/split/render/summarize/compress/e2e/degrade)通过。`compress_messages` 行为不变,现有用例无改动即通过。

- [ ] **Step 5: 提交**

```bash
git add twinkle/agentserver/compression/__init__.py tests/test_context_compression.py
git commit -m "refactor(compression): split compress_messages into should_compress + do_compress"
```

---

### Task 2: 新增属性常量

**Files:**
- Modify: `twinkle/observability/attributes.py`(追加 span 名 + key)
- Test: `tests/test_observability.py`(扩展 `test_attribute_constants_are_strings`)

**Interfaces:**
- Produces: `A.SPAN_COMPRESSION`、`A.SPAN_SKILL_EVOLUTION`、`A.TWINKLE_COMPRESSION_*`、`A.TWINKLE_SKILL_NAME`、`A.TWINKLE_EVOLUTION_STATUS`、`A.TWINKLE_EVOLUTION_MESSAGE`(Task 3/4 引用)

- [ ] **Step 1: 写失败测试**

在 `tests/test_observability.py` 的 `test_attribute_constants_are_strings` 末尾追加断言:

```python
def test_attribute_constants_are_strings():
    assert A.SPAN_AGENT_INVOKE == "twinkle.agent.invoke"
    assert A.SPAN_GEN_AI_CHAT == "gen_ai.chat"
    assert A.SPAN_GEN_AI_TOOL == "gen_ai.tool"
    assert A.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert A.METRIC_TOKEN_USAGE == "gen_ai.client.token.usage"
    assert A.TOOL_ERROR_PREFIX == "[tool error]"
    # --- new: compression + evolution ---
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_observability.py::test_attribute_constants_are_strings -v`
Expected: FAIL — `AttributeError: module 'twinkle.observability.attributes' has no attribute 'SPAN_COMPRESSION'`

- [ ] **Step 3: 加常量**

在 `twinkle/observability/attributes.py` 的 `# --- span names ---` 块追加,并在 `# --- twinkle.* (custom) ---` 块末尾追加:

```python
# --- span names ---
SPAN_AGENT_INVOKE = "twinkle.agent.invoke"
SPAN_GEN_AI_CHAT = "gen_ai.chat"
SPAN_GEN_AI_TOOL = "gen_ai.tool"
SPAN_COMPRESSION = "twinkle.compression"
SPAN_SKILL_EVOLUTION = "twinkle.skill.evolution"
```

```python
# --- twinkle.* (custom) ---
TWINKLE_REQUEST_ID = "twinkle.request.id"
TWINKLE_SESSION_ID = "twinkle.session.id"
TWINKLE_AGENT_ITERATIONS = "twinkle.agent.iterations"
TWINKLE_AGENT_STATUS = "twinkle.agent.status"
# --- compression ---
TWINKLE_COMPRESSION_TOKENS_BEFORE = "twinkle.compression.tokens_before"
TWINKLE_COMPRESSION_TOKENS_AFTER = "twinkle.compression.tokens_after"
TWINKLE_COMPRESSION_COMPRESSED = "twinkle.compression.compressed"
TWINKLE_COMPRESSION_HAS_SUMMARY = "twinkle.compression.has_summary"
TWINKLE_COMPRESSION_STRATEGY = "twinkle.compression.strategy"
# --- skill / evolution ---
TWINKLE_SKILL_NAME = "twinkle.skill.name"
TWINKLE_EVOLUTION_STATUS = "twinkle.evolution.status"
TWINKLE_EVOLUTION_MESSAGE = "twinkle.evolution.message"
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_observability.py::test_attribute_constants_are_strings -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add twinkle/observability/attributes.py tests/test_observability.py
git commit -m "feat(observability): add compression + evolution span/attr constants"
```

---

### Task 3: 压缩 instrumentor(patch `do_compress`)

**Files:**
- Create: `twinkle/observability/instrumentors/compression.py`
- Test: `tests/test_observability_compression_evolution.py`(新建,含内联 fixture)

**Interfaces:**
- Consumes: Task 1 的 `do_compress`(同模块被 `compress_messages` 调用);Task 2 的 `A.SPAN_COMPRESSION` 等;`_stamp_ctx` from `instrumentors.llm`
- Produces: `instrument_compression(tracer, metrics, cfg, *, compression_mod=None) -> bool` — patch `compression_mod.do_compress`

- [ ] **Step 1: 写失败测试(新建测试文件,含 fixture + 3 个压缩测试)**

新建 `tests/test_observability_compression_evolution.py`:

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_observability_compression_evolution.py -v`
Expected: FAIL — `ImportError: cannot import name 'instrument_compression'` (and `instrument_evolution` — Task 4 还没写;先把 evolution import 行注释掉以独立跑压缩测试,或先只跑压缩测试)

> 注:文件顶部 import 了 `instrument_evolution`(Task 4)与 `apply_instrumentors` 注册(Task 5 前 results 无新 key)。为让本任务独立可跑,临时把 `from twinkle.observability.instrumentors.evolution import instrument_evolution` 注释掉;Task 4 完成后取消注释。本步骤先跑:
> `python -m pytest tests/test_observability_compression_evolution.py::test_compression_real_path_noop_fire_degrade tests/test_observability_compression_evolution.py::test_compression_idempotent -v`
> Expected: FAIL — `ImportError: cannot import name 'instrument_compression'`

- [ ] **Step 3: 实现 `instrument_compression`**

新建 `twinkle/observability/instrumentors/compression.py`:

```python
"""Instrument compression.do_compress -> twinkle.compression span.

Patches ``do_compress`` (not ``compress_messages``) on the ``compression``
module. ``do_compress`` is only called when ``should_compress`` is True, so the
wrapper always opens a span with no false positives and no prediction logic.
Because ``do_compress`` is a same-module callee of ``compress_messages``
(resolved via module globals at call time, not import-bound), patching it
reaches both production call sites (ContextCompressionHook +
ContextOverflowRecoveryHook) with zero hook changes. The summary ``llm.stream``
inside ``_summarize`` emits a ``gen_ai.chat`` span that nests under this span
(span is current via ``start_as_current_span``).
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx


def _has_summary(msgs: list[dict]) -> bool:
    """True if the returned messages contain a ``[prior context summary]`` system msg.

    Distinguishes the normal summary path from the ``_summarize``-failed degrade
    path (head + tail, middle dropped, no summary message).
    """
    for m in msgs:
        if (m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[prior context summary]")):
            return True
    return False


def instrument_compression(tracer, metrics, cfg, *, compression_mod=None) -> bool:
    """Patch ``compression.do_compress`` to emit a ``twinkle.compression`` span.

    ``metrics`` is accepted for signature parity with sibling instrumentors but
    unused (compression is low-frequency; spans suffice).
    """
    if compression_mod is None:
        from twinkle.agentserver import compression as compression_mod

    estimate_tokens = compression_mod.estimate_tokens

    def factory(original):
        async def traced(msgs, llm, *, keep_recent_pairs, summary_system_prompt):
            before_tokens = estimate_tokens(msgs)
            with tracer.start_as_current_span(A.SPAN_COMPRESSION) as span:
                _stamp_ctx(span)
                span.set_attribute(A.TWINKLE_COMPRESSION_TOKENS_BEFORE, before_tokens)
                try:
                    result = await original(
                        msgs, llm,
                        keep_recent_pairs=keep_recent_pairs,
                        summary_system_prompt=summary_system_prompt,
                    )
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                after_tokens = estimate_tokens(result)
                span.set_attribute(A.TWINKLE_COMPRESSION_TOKENS_AFTER, after_tokens)
                span.set_attribute(A.TWINKLE_COMPRESSION_COMPRESSED,
                                   after_tokens < before_tokens)
                span.set_attribute(A.TWINKLE_COMPRESSION_HAS_SUMMARY,
                                   _has_summary(result))
                span.set_attribute(A.TWINKLE_COMPRESSION_STRATEGY, "inline_summary")
                return result
        return traced

    from twinkle.observability.wrap import patch_method
    return patch_method(compression_mod, "do_compress", factory)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_observability_compression_evolution.py::test_compression_real_path_noop_fire_degrade tests/test_observability_compression_evolution.py::test_compression_idempotent -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add twinkle/observability/instrumentors/compression.py tests/test_observability_compression_evolution.py
git commit -m "feat(observability): instrument compression.do_compress -> twinkle.compression span"
```

---

### Task 4: 进化 instrumentor(patch `evolve`)

**Files:**
- Create: `twinkle/observability/instrumentors/evolution.py`
- Test: `tests/test_observability_compression_evolution.py`(追加,并取消 Task 3 临时注释的 import)

**Interfaces:**
- Consumes: Task 2 的 `A.SPAN_SKILL_EVOLUTION` 等;`_stamp_ctx`/`_trunc` from `instrumentors.llm`;`OnlineEvolutionOrchestrator.evolve(self, skill_name, conversation_messages, signals=None, skill_content=None) -> EvolutionResult`(签名以 `orchestrator.py:39` 为准)
- Produces: `instrument_evolution(tracer, metrics, cfg, *, orchestrator_cls=None) -> bool`

- [ ] **Step 1: 写失败测试**

在 `tests/test_observability_compression_evolution.py` 末尾追加。先取消文件顶部 `from twinkle.observability.instrumentors.evolution import instrument_evolution` 的注释(Task 3 临时注释的)。

```python
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
    assert instrument_evolution(tracer, Metrics(None), _Cfg(),
                                orchestrator_cls=_FakeEvoOrchestrator) is True
    assert instrument_evolution(tracer, Metrics(None), _Cfg(),
                                orchestrator_cls=_FakeEvoOrchestrator) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_observability_compression_evolution.py::test_evolution_span_with_status -v`
Expected: FAIL — `ImportError: cannot import name 'instrument_evolution'`

- [ ] **Step 3: 实现 `instrument_evolution`**

新建 `twinkle/observability/instrumentors/evolution.py`:

```python
"""Instrument OnlineEvolutionOrchestrator.evolve -> twinkle.skill.evolution span.

Patches ``evolve`` (per-skill, contains the signal-detection + experience-
generation LLM calls, returns an ``EvolutionResult`` with ``.status``). The
internal LLM calls' ``gen_ai.chat`` spans nest under this span (current via
``start_as_current_span``). ``run_feedback_loop`` is NOT patched — it returns
None (no status), so a span there has low diagnostic value; its LLM calls stay
indistinguishable ``gen_ai.chat`` (accepted, YAGNI).
"""
from __future__ import annotations

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx, _trunc


def instrument_evolution(tracer, metrics, cfg, *, orchestrator_cls=None) -> bool:
    """Patch ``OnlineEvolutionOrchestrator.evolve`` to emit a
    ``twinkle.skill.evolution`` span carrying ``skill.name`` / ``evolution.status``
    / ``evolution.message``.

    ``metrics`` is accepted for signature parity but unused (evolution is
    low-frequency; spans suffice).
    """
    if orchestrator_cls is None:
        from twinkle.agentserver.evolution.orchestrator import (
            OnlineEvolutionOrchestrator as orchestrator_cls,
        )

    def factory(original):
        async def traced(self, skill_name, conversation_messages, *args, **kwargs):
            with tracer.start_as_current_span(A.SPAN_SKILL_EVOLUTION) as span:
                _stamp_ctx(span)
                span.set_attribute(A.TWINKLE_SKILL_NAME, skill_name or "")
                try:
                    result = await original(
                        self, skill_name, conversation_messages, *args, **kwargs
                    )
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise
                if result is not None:
                    span.set_attribute(A.TWINKLE_EVOLUTION_STATUS,
                                       getattr(result, "status", "") or "")
                    span.set_attribute(A.TWINKLE_EVOLUTION_MESSAGE,
                                       _trunc(getattr(result, "message", "") or ""))
                return result
        return traced

    from twinkle.observability.wrap import patch_method
    return patch_method(orchestrator_cls, "evolve", factory)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_observability_compression_evolution.py -v`
Expected: PASS — 全部(compression 2 个 + evolution 3 个,共 5 个)。

- [ ] **Step 5: 提交**

```bash
git add twinkle/observability/instrumentors/evolution.py tests/test_observability_compression_evolution.py
git commit -m "feat(observability): instrument OnlineEvolutionOrchestrator.evolve -> twinkle.skill.evolution span"
```

---

### Task 5: 在 `apply_instrumentors` 注册两条 + 更新现有 results 断言

**Files:**
- Modify: `twinkle/observability/instrumentors/__init__.py`(注册 compression + evolution,加两个参数)
- Test: `tests/test_observability_compression_evolution.py`(注册测试)
- Modify: `tests/test_observability.py`(更新 `test_full_trace_tree` 与 `test_subagent_invoke_span_nests_under_tool_span` 的 results 断言)

**Interfaces:**
- Consumes: Task 3 `instrument_compression`、Task 4 `instrument_evolution`
- Produces: `apply_instrumentors(..., *, compression_mod=None, orchestrator_cls=None)` 返回 dict 含 `compression`/`evolution` key

- [ ] **Step 1: 写失败测试(注册 + 更新现有断言)**

在 `tests/test_observability_compression_evolution.py` 末尾追加注册测试(用全 fake,不 patch 真实模块):

```python
class _NoopAgent:
    async def run(self, request):
        yield "f"


class _NoopLLM:
    def __init__(self):
        self._model = "noop"


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
```

更新 `tests/test_observability.py` 中两处 results 断言(3 key → 5 key):

在 `test_full_trace_tree` 中把:
```python
    assert results == {"agent": True, "llm": True, "tool": True}
```
改为:
```python
    assert results["agent"] is True
    assert results["llm"] is True
    assert results["tool"] is True
    assert results["compression"] is True
    assert results["evolution"] is True
```

在 `test_subagent_invoke_span_nests_under_tool_span` 中同样把:
```python
    assert results == {"agent": True, "llm": True, "tool": True}
```
改为:
```python
    assert results["agent"] is True
    assert results["llm"] is True
    assert results["tool"] is True
    assert results["compression"] is True
    assert results["evolution"] is True
```

> 注:这两个现有测试不传 `compression_mod`/`orchestrator_cls`,故走默认懒导入,patch 真实 `compression.do_compress` 与 `OnlineEvolutionOrchestrator.evolve`。真实模块 patch 是幂等的、对这两个测试无副作用(它们不断言 compression/evolution 的 span,只断言 results 全 True)。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_observability_compression_evolution.py::test_apply_instrumentors_registers_compression_and_evolution tests/test_observability.py::test_full_trace_tree tests/test_observability.py::test_subagent_invoke_span_nests_under_tool_span -v`
Expected: FAIL — 注册测试 `TypeError: apply_instrumentors() got an unexpected keyword argument 'compression_mod'`;两个现有测试 results 断言缺 key。

- [ ] **Step 3: 实现注册**

修改 `twinkle/observability/instrumentors/__init__.py`,把整个 `apply_instrumentors` 替换为:

```python
def apply_instrumentors(tracer, metrics, cfg, *, agent_cls=None, llm_cls=None,
                        tool_cls=None, compression_mod=None,
                        orchestrator_cls=None):
    from twinkle.observability.instrumentors.agent import instrument_agent
    from twinkle.observability.instrumentors.llm import instrument_llm
    from twinkle.observability.instrumentors.tool import instrument_tool
    from twinkle.observability.instrumentors.compression import instrument_compression
    from twinkle.observability.instrumentors.evolution import instrument_evolution

    results = {}
    for label, fn in (
        ("agent", lambda: instrument_agent(tracer, metrics, cfg, agent_cls=agent_cls)),
        ("llm", lambda: instrument_llm(tracer, metrics, cfg, llm_cls=llm_cls)),
        ("tool", lambda: instrument_tool(tracer, metrics, cfg, tool_cls=tool_cls)),
        ("compression", lambda: instrument_compression(tracer, metrics, cfg, compression_mod=compression_mod)),
        ("evolution", lambda: instrument_evolution(tracer, metrics, cfg, orchestrator_cls=orchestrator_cls)),
    ):
        try:
            results[label] = fn()
        except Exception:
            log.exception("instrumentor %s failed", label)
            results[label] = False
    return results
```

- [ ] **Step 4: 运行全量确认通过**

Run: `python -m pytest tests/test_observability.py tests/test_observability_compression_evolution.py tests/test_context_compression.py -v`
Expected: PASS — 全部。

- [ ] **Step 5: 提交**

```bash
git add twinkle/observability/instrumentors/__init__.py tests/test_observability_compression_evolution.py tests/test_observability.py
git commit -m "feat(observability): register compression + evolution instrumentors in apply_instrumentors"
```

---

## Self-Review

**Spec coverage:**
- §1 压缩 instrumentor(拆 `should_compress`+`do_compress`,patch `do_compress`,预测式→实为"do_compress 只在真压缩时被调故无假阳性",wrapper 属性 tokens_before/after/compressed/has_summary/strategy)→ Task 1 + Task 3 ✓
- §2 进化 instrumentor(patch `evolve`,status/skill_name/message,不 patch run_feedback_loop)→ Task 4 ✓
- §3 属性常量 → Task 2 ✓
- §4 注册接线(`apply_instrumentors` + 两参数)→ Task 5 ✓
- §5 测试(no-op/fire+nested chat/degrade/两路径/idempotent/evolve status/evolution error)→ Task 3/4/5 覆盖;"两条调用路径"由 Task 3 patch 同模块 `do_compress` 自动覆盖(设计本身保证,集成测试在真实 `compress_messages` 上跑即验证) ✓
- 回归 `tests/test_context_compression.py` → Task 1 Step 4 ✓

**Placeholder scan:** 无 TBD/TODO/"add appropriate";所有代码块完整。

**Type consistency:** `should_compress(msgs, *, token_threshold, keep_recent_pairs) -> bool`、`do_compress(msgs, llm, *, keep_recent_pairs, summary_system_prompt)`、`instrument_compression(..., *, compression_mod=None)`、`instrument_evolution(..., *, orchestrator_cls=None)`、`apply_instrumentors(..., *, compression_mod=None, orchestrator_cls=None)` — 各任务引用一致。`A.SPAN_COMPRESSION`/`A.SPAN_SKILL_EVOLUTION`/`A.TWINKLE_*` 在 Task 2 定义,Task 3/4 引用名称一致。`EvolutionResult(status, skill_name, message)` 与 `orchestrator.py:17-22` 一致。`evolve(self, skill_name, conversation_messages, *args, **kwargs)` 透传与 `orchestrator.py:39-41` 一致。

**已知取舍(非缺陷):**
- Task 3 集成测试 patch 真实 `compression.do_compress`,测试后真实模块被 patch(幂等、tracer 关停后为 no-op span,`compress_messages` 行为不变)→ `tests/test_context_compression.py` 同进程跑仍 PASS(行为不变,仅 no-op span 开销)。可接受,符合现有 `test_observability.py` 在真实模块上跑集成测试的做法。
- Task 5 两个现有测试走默认懒导入 patch 真实 compression/evolution 模块(幂等),无副作用。
