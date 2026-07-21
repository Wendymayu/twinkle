# Observability Module (agentserver, OTel + monkey-patch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `twinkle/agentserver` 加一个 in-tree 可观测模块，用 OpenTelemetry 采 traces + metrics、OTLP/gRPC 导出到远程 collector；monkey-patch 3 个 choke point，业务代码零插桩调用。

**Architecture:** 新建 `twinkle/observability/`（依赖方向单向 observability→agentserver，agentserver 永不 import observability）。启动时 `setup()` 读 env，`OTEL_ENABLED=true` 则 `init_providers` 造 tracer/meter + `apply_instrumentors` 用幂等 fail-soft 的 `patch_method` 包 `AgentLoop.run_stream` / `LLMClient.stream` / `ToolManager.execute`，产 span 树 `twinkle.agent.invoke → {gen_ai.chat, gen_ai.tool}`。`OTEL_ENABLED=false`（默认）零成本 no-op。唯一业务改动：`llm_client.py` 的 `Finish` 加 `usage` 字段（暴露数据，非插桩调用）。

**Tech Stack:** Python ≥3.11；OpenTelemetry SDK（`opentelemetry-api` / `-sdk` / `-exporter-otlp-proto-grpc`，放 `[obs]` optional extra）；pytest（既有）；monkey-patch（非 DI）。

**参考 spec:** `docs/superpowers/specs/2026-07-21-observability-module-design.md`

---

## File Structure

新建文件：
```
twinkle/observability/
  __init__.py            # setup() 单入口（config→provider→apply_instrumentors，幂等 fail-soft）
  config.py              # ObservabilityConfig + load_config()（OTEL_* + TWINKLE_OBS_* env）
  provider.py            # init_providers(cfg) -> (tracer, meter)；OTLP gRPC/console/none；不设全局
  attributes.py          # span/metric 属性键常量（gen_ai.* semconv + twinkle.*）
  wrap.py                # patch_method(cls, name, factory)：幂等、fail-soft、__wrapped__
  context.py             # RequestContext ContextVar + _llm_call_counter
  metrics.py             # Metrics 类：counters/histograms + fail-soft record_*
  instrumentors/
    __init__.py           # apply_instrumentors(tracer, metrics, cfg, *, agent_cls=, llm_cls=, tool_cls=)
    agent.py              # instrument_agent：包 AgentLoop.run_stream → twinkle.agent.invoke（root）
    llm.py                # instrument_llm：包 LLMClient.stream → gen_ai.chat
    tool.py               # instrument_tool：包 ToolManager.execute → gen_ai.tool
tests/test_observability.py   # 所有 obs 测试 + in-memory fixtures（不进 root conftest）
```

修改文件：
- `pyproject.toml` — 加 `[obs]` optional extra。
- `.env.example` — 追加 OTEL_* 块（注释默认关）。
- `twinkle/agentserver/llm_client.py` — `Finish` 加 `usage: dict | None = None`；`stream()` 加 `stream_options` + 捕获 usage。
- `twinkle/agentserver/__main__.py` — `main()` 之前加一行 `twinkle.observability.setup()`。

> **设计偏离 spec 之处（更优，已确认）：** (1) in-memory 测试 fixtures 放 `tests/test_observability.py`（带 `pytest.importorskip`），**不进 root `tests/conftest.py`**——这样未装 `[obs]` 时既有测试零影响（零回炉）。(2) `init_providers` 返回 `(tracer, meter)` 但**不调 `set_tracer_provider`/`set_meter_provider`**——instrumentors 拿 tracer 当参数，不依赖全局，测试无全局污染。

---

### Task 1: 加 `[obs]` 可选依赖 + `.env.example`

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`

- [ ] **Step 1: 改 `pyproject.toml`，加 `obs` extra**

把 `[project.optional-dependencies]` 段从：
```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
```
改为：
```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
obs = [
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
]
```

- [ ] **Step 2: 在 `.env.example` 末尾追加 OTEL 块**

追加（带注释，默认关）：
```ini
# --- observability (OTel, agentserver-only; default off = zero-cost no-op) ---
# OTEL_ENABLED=true
# OTEL_TRACES_EXPORTER=otlp
# OTEL_METRICS_EXPORTER=otlp
# OTEL_EXPORTER_OTLP_PROTOCOL=grpc
# OTEL_EXPORTER_OTLP_ENDPOINT=http://101.37.215.110:4317
# OTEL_SERVICE_NAME=twinkle-agentserver
# TWINKLE_OBS_CAPTURE_MESSAGES=false
```

- [ ] **Step 3: 装 `[obs]` 并验证可 import**

Run: `python -m pip install -e ".[obs]"`
Run: `python -c "import opentelemetry; print(opentelemetry.__version__)"`
Expected: 打印一个版本号（如 `1.x.x`），无报错。

- [ ] **Step 4: 确认既有测试不受影响（未启用 obs 时）**

Run: `python -m pytest tests/ -q`
Expected: 全绿（obs 还没动任何业务代码）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example
git commit -m "obs: add [obs] optional extra + OTEL env example"
```

---

### Task 2: §6 唯一业务改动 — `llm_client.py` 暴露 `Finish.usage`

> 先于 instrumentors 完成：`instrument_llm` 要读 `ev.usage`，故 `Finish.usage` 必须先存在。observability-agnostic（只暴露已有数据，无插桩调用）。

**Files:**
- Modify: `twinkle/agentserver/llm_client.py`
- Test: `tests/test_llm_client.py`

- [ ] **Step 1: 扩展 fake `_Chunk` 支持 `usage`（测试侧，向后兼容）**

在 `tests/test_llm_client.py` 把：
```python
class _Chunk:
    def __init__(self, choices):
        self.choices = choices
```
改为：
```python
class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage
```

- [ ] **Step 2: 写失败测试 — usage 透传到 `Finish`**

在 `tests/test_llm_client.py` 末尾加：
```python
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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_llm_client.py::test_trailing_usage_chunk_is_captured_on_finish -v`
Expected: FAIL — `Finish` 无 `usage` 字段（`AttributeError` 或 `assert None == {...}`）。

- [ ] **Step 4: 改 `llm_client.py` — `Finish` 加字段 + 捕获 usage**

`Finish` dataclass 从：
```python
@dataclass
class Finish:
    finish_reason: str
    assistant_message: dict
```
改为：
```python
@dataclass
class Finish:
    finish_reason: str
    assistant_message: dict
    usage: dict | None = None
```

`stream()` 内：kwargs 从
```python
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
```
改为（加 `stream_options`）：
```python
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
```

循环体顶部、`if not chunk.choices: continue` **之前**，捕获 usage。把
```python
        async for chunk in stream:
            # OpenAI-compatible streams ... Skip it ...
            if not chunk.choices:
                continue
```
改为：
```python
        usage: dict | None = None
        async for chunk in stream:
            # Capture token usage if the provider emits it (OpenAI with
            # stream_options.include_usage, or dashscope) — some providers
            # attach usage to the last content chunk, others to a trailing
            # usage-only chunk with empty choices. Last non-null wins.
            _u = getattr(chunk, "usage", None)
            if _u:
                usage = _u
            # OpenAI-compatible streams ... Skip the usage-only chunk ...
            if not chunk.choices:
                continue
```

最后 `yield Finish(...)` 从：
```python
        yield Finish(
            finish_reason=finish_reason,
            assistant_message={
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
        )
```
改为：
```python
        yield Finish(
            finish_reason=finish_reason,
            assistant_message={
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
            usage=usage,
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_llm_client.py -v`
Expected: 全绿（含新测试 + 3 个既有测试零回炉——`Finish.usage` 默认 `None` 不影响既有断言）。

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/llm_client.py tests/test_llm_client.py
git commit -m "obs: expose Finish.usage in llm_client (stream_options + capture)"
```

### Task 3: `attributes.py` — span/metric 属性键常量

**Files:**
- Create: `twinkle/observability/__init__.py`
- Create: `twinkle/observability/attributes.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 建包 `twinkle/observability/__init__.py`（空占位）**

Create `twinkle/observability/__init__.py` with only a docstring for now:
```python
"""twinkle.observability — agentserver observability (OTel + monkey-patch).

Public entry point: setup(). (Implemented in Task 12.)
"""
```

- [ ] **Step 2: 写失败测试 — 常量是预期字符串**

Create `tests/test_observability.py`:
```python
import pytest

# Skip the whole file gracefully if [obs] isn't installed — keeps the
# existing test suite green without opentelemetry.
pytest.importorskip("opentelemetry.sdk")

from twinkle.observability import attributes as A


def test_attribute_constants_are_strings():
    assert A.SPAN_AGENT_INVOKE == "twinkle.agent.invoke"
    assert A.SPAN_GEN_AI_CHAT == "gen_ai.chat"
    assert A.SPAN_GEN_AI_TOOL == "gen_ai.tool"
    assert A.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert A.METRIC_TOKEN_USAGE == "gen_ai.client.token.usage"
    assert A.TOOL_ERROR_PREFIX == "[tool error]"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py::test_attribute_constants_are_strings -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.attributes`。

- [ ] **Step 4: 实现 `attributes.py`**

Create `twinkle/observability/attributes.py`:
```python
"""Span/metric attribute key constants.

Aligned with OpenTelemetry GenAI semantic conventions (gen_ai.*) plus
twinkle-specific dimensions (twinkle.*). Centralized so instrumentors
never hardcode string keys.
"""

# --- span names ---
SPAN_AGENT_INVOKE = "twinkle.agent.invoke"
SPAN_GEN_AI_CHAT = "gen_ai.chat"
SPAN_GEN_AI_TOOL = "gen_ai.tool"

# --- gen_ai.* (OTel GenAI semconv) ---
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reason"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_STREAMING_FIRST_TOKEN_MS = "gen_ai.streaming.first_token_ms"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_ERROR = "gen_ai.tool.error"
GEN_AI_TOOL_ARGUMENTS = "gen_ai.tool.arguments"
GEN_AI_TOOL_RESULT = "gen_ai.tool.result"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"

# --- twinkle.* (custom) ---
TWINKLE_REQUEST_ID = "twinkle.request.id"
TWINKLE_SESSION_ID = "twinkle.session.id"
TWINKLE_AGENT_ITERATIONS = "twinkle.agent.iterations"
TWINKLE_AGENT_STATUS = "twinkle.agent.status"

# --- metric names ---
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_TOOL_COUNT = "gen_ai.tool.count"
METRIC_LLM_DURATION = "gen_ai.client.operation.duration"
METRIC_TOOL_DURATION = "gen_ai.tool.duration"
METRIC_AGENT_DURATION = "twinkle.agent.duration"

# --- misc ---
TOOL_ERROR_PREFIX = "[tool error]"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py::test_attribute_constants_are_strings -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add twinkle/observability/__init__.py twinkle/observability/attributes.py tests/test_observability.py
git commit -m "obs: add attributes constants + test scaffolding"
```

---

### Task 4: `wrap.py` — `patch_method`（幂等、fail-soft）

**Files:**
- Create: `twinkle/observability/wrap.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — 4 个行为（包住调原方法 / 幂等 / 缺方法 fail-soft / factory 抛错 fail-soft）**

在 `tests/test_observability.py` 末尾加：
```python
import asyncio

from twinkle.observability.wrap import patch_method


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
```

> 每个测试用**局部类**（不共享模块级状态），避免 monkey-patch 跨测试污染。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k patch -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.wrap`。

- [ ] **Step 3: 实现 `wrap.py`**

Create `twinkle/observability/wrap.py`:
```python
"""patch_method — idempotent, fail-soft monkey-patch helper.

Borrowed from jiuwenswarm-instrumentor wrap.py. Marks wrappers with
_twinkle_wrapped so repeated patches are no-ops; any failure is logged
and skipped, never raised into the host.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("twinkle.observability.wrap")

_WRAPPED_MARKER = "_twinkle_wrapped"


def patch_method(cls: type, name: str, factory: Callable[[Any], Any]) -> bool:
    """Patch ``cls.<name>`` with ``factory(original)``.

    Idempotent (already-wrapped -> no-op) and fail-soft (any error -> log +
    skip, never raise into the host). Returns True if patched, False if
    skipped.
    """
    try:
        original = getattr(cls, name)
    except AttributeError:
        log.warning("patch_method: %s.%s not found; skip", cls.__name__, name)
        return False
    if getattr(original, _WRAPPED_MARKER, False):
        return False
    try:
        wrapped = factory(original)
        setattr(wrapped, _WRAPPED_MARKER, True)
        setattr(wrapped, "__wrapped__", original)
        setattr(cls, name, wrapped)
        return True
    except Exception:
        log.exception("patch_method: failed to wrap %s.%s; skip", cls.__name__, name)
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k patch -v`
Expected: PASS（4 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/wrap.py tests/test_observability.py
git commit -m "obs: add idempotent fail-soft patch_method"
```

### Task 5: `context.py` — request 上下文 + LLM 调用计数

**Files:**
- Create: `twinkle/observability/context.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — set/reset + 计数**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.observability.context import (
    set_request_context,
    current_request_context,
    reset_llm_counter,
    increment_llm_counter,
    current_llm_counter,
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k "request_context or llm_counter" -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.context`。

- [ ] **Step 3: 实现 `context.py`**

Create `twinkle/observability/context.py`:
```python
"""Request-scoped context for stamping ids onto child spans.

set_request_context(...) returns a token whose reset() goes in finally;
_llm_call_counter is reset by the agent wrap and incremented by the llm
wrap so the agent wrap can stamp twinkle.agent.iterations at span end.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class RequestContext:
    request_id: str | None = None
    session_id: str | None = None
    agent_name: str | None = None


_request_context: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "twinkle_obs_request_context", default=None
)
_llm_call_counter: contextvars.ContextVar[int] = contextvars.ContextVar(
    "twinkle_obs_llm_counter", default=0
)


class _Token:
    def __init__(self, var: contextvars.ContextVar, token: contextvars.Token) -> None:
        self._var = var
        self._token = token

    def reset(self) -> None:
        self._var.reset(self._token)


def set_request_context(**kwargs) -> _Token:
    return _Token(_request_context, _request_context.set(RequestContext(**kwargs)))


def current_request_context() -> RequestContext | None:
    return _request_context.get()


def reset_llm_counter() -> _Token:
    return _Token(_llm_call_counter, _llm_call_counter.set(0))


def increment_llm_counter() -> None:
    _llm_call_counter.set(_llm_call_counter.get() + 1)


def current_llm_counter() -> int:
    return _llm_call_counter.get()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k "request_context or llm_counter" -v`
Expected: PASS（2 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/context.py tests/test_observability.py
git commit -m "obs: add request context + llm call counter"
```

---

### Task 6: `config.py` — env 驱动、默认关

**Files:**
- Create: `twinkle/observability/config.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — 默认关 + 读 env**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.observability.config import load_config

_OBS_KEYS = [
    "OTEL_ENABLED", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME",
    "TWINKLE_OBS_CAPTURE_MESSAGES",
]


def test_config_defaults_off(monkeypatch):
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
    assert cfg.capture_messages is False


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "twinkle-agentserver")
    monkeypatch.setenv("TWINKLE_OBS_CAPTURE_MESSAGES", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "k1=v1, k2=v2")
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.traces_exporter == "otlp"
    assert cfg.metrics_exporter == "otlp"
    assert cfg.protocol == "grpc"
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.headers == {"k1": "v1", "k2": "v2"}
    assert cfg.capture_messages is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k config -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.config`。

- [ ] **Step 3: 实现 `config.py`**

Create `twinkle/observability/config.py`:
```python
"""ObservabilityConfig — env-driven, default-off.

Mirrors jiuwenswarm-instrumentor config.py. OTEL_ENABLED=false (default)
=> setup() is a zero-cost no-op. Importing twinkle.config triggers the
repo-root .env loader (side effect) so os.getenv sees .env values too.
"""
from __future__ import annotations

import os

import twinkle.config  # noqa: F401 — triggers .env loading


def _get_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _get_headers(key: str) -> dict[str, str]:
    raw = os.getenv(key, "")
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
    return out


class ObservabilityConfig:
    def __init__(self) -> None:
        self.enabled = _get_bool("OTEL_ENABLED", False)
        self.traces_exporter = os.getenv("OTEL_TRACES_EXPORTER", "none").lower()
        self.metrics_exporter = os.getenv("OTEL_METRICS_EXPORTER", "none").lower()
        self.protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()
        self.endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self.headers = _get_headers("OTEL_EXPORTER_OTLP_HEADERS")
        self.service_name = os.getenv("OTEL_SERVICE_NAME", "twinkle-agentserver")
        self.capture_messages = _get_bool("TWINKLE_OBS_CAPTURE_MESSAGES", False)


def load_config() -> ObservabilityConfig:
    return ObservabilityConfig()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k config -v`
Expected: PASS（2 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/config.py tests/test_observability.py
git commit -m "obs: add env-driven ObservabilityConfig (default off)"
```

---

### Task 7: 测试基础设施 — `CollectingSpanExporter` + fixtures

> 放进 `tests/test_observability.py`（不进 root conftest，避免未装 `[obs]` 时污染既有测试）。被 Task 8+ 的 instrumentor/metrics 测试复用。

**Files:**
- Modify: `tests/test_observability.py`

- [ ] **Step 1: 写 smoke 测试（先要 fixture）**

在 `tests/test_observability.py` 末尾加：
```python
def test_tracer_exporter_collects_spans(tracer_exporter):
    tracer, exp = tracer_exporter
    with tracer.start_as_current_span("smoke") as span:
        span.set_attribute("k", "v")
    assert len(exp.spans) == 1
    assert exp.spans[0].name == "smoke"
    assert exp.spans[0].attributes["k"] == "v"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py::test_tracer_exporter_collects_spans -v`
Expected: FAIL — `fixture 'tracer_exporter' not found`。

- [ ] **Step 3: 实现 fixtures**

在 `tests/test_observability.py` **顶部**（`import pytest` 之后、`pytest.importorskip(...)` 之前——importorskip 必须先跑，故 OTel import 放在它之后）插入：
```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider, InMemoryMetricReader


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
```

> `SimpleSpanProcessor` 同步导出，span 在 `end()` 时立刻进 `exp.spans`，测试可即时断言。不设全局 provider（instrumentors 拿 tracer 当参数）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py::test_tracer_exporter_collects_spans -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/test_observability.py
git commit -m "obs: add CollectingSpanExporter + tracer/meter fixtures"
```

### Task 8: `metrics.py` — fail-soft counters/histograms

**Files:**
- Create: `twinkle/observability/metrics.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — 记录 token / 工具 / fail-soft**

在 `tests/test_observability.py` 末尾加：
```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k metrics -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.metrics`。

- [ ] **Step 3: 实现 `metrics.py`**

Create `twinkle/observability/metrics.py`:
```python
"""Metrics — fail-soft wrappers over OTel counters/histograms."""
from __future__ import annotations

import logging

from twinkle.observability import attributes as A

log = logging.getLogger("twinkle.observability.metrics")


class Metrics:
    def __init__(self, meter) -> None:
        self._meter = meter
        self._token_usage = self._create_counter(A.METRIC_TOKEN_USAGE, "LLM token usage", "token")
        self._tool_count = self._create_counter(A.METRIC_TOOL_COUNT, "Tool invocations", "1")
        self._llm_duration = self._create_histogram(A.METRIC_LLM_DURATION, "LLM call duration", "s")
        self._tool_duration = self._create_histogram(A.METRIC_TOOL_DURATION, "Tool call duration", "s")
        self._agent_duration = self._create_histogram(A.METRIC_AGENT_DURATION, "Agent invoke duration", "s")

    def _create_counter(self, name, desc, unit):
        try:
            return self._meter.create_counter(name, unit=unit, description=desc)
        except Exception:
            log.exception("create_counter failed: %s", name)
            return None

    def _create_histogram(self, name, desc, unit):
        try:
            return self._meter.create_histogram(name, unit=unit, description=desc)
        except Exception:
            log.exception("create_histogram failed: %s", name)
            return None

    def record_token_usage(self, usage: dict | None, model: str) -> None:
        if not usage or not self._token_usage:
            return
        try:
            attrs = {A.GEN_AI_REQUEST_MODEL: model or "unknown"}
            inp = usage.get("prompt_tokens") or usage.get("input_tokens")
            out = usage.get("completion_tokens") or usage.get("output_tokens")
            tot = usage.get("total_tokens")
            if inp is not None:
                self._token_usage.add(int(inp), {**attrs, A.GEN_AI_TOKEN_TYPE: "input"})
            if out is not None:
                self._token_usage.add(int(out), {**attrs, A.GEN_AI_TOKEN_TYPE: "output"})
            if tot is not None and inp is None and out is None:
                self._token_usage.add(int(tot), {**attrs, A.GEN_AI_TOKEN_TYPE: "total"})
        except Exception:
            log.exception("record_token_usage failed")

    def record_tool_call(self, name: str, error: bool, duration_s: float) -> None:
        if not self._tool_count or not self._tool_duration:
            return
        try:
            attrs = {A.GEN_AI_TOOL_NAME: name or "unknown", A.GEN_AI_TOOL_ERROR: error}
            self._tool_count.add(1, attrs)
            self._tool_duration.record(duration_s, {A.GEN_AI_TOOL_NAME: name or "unknown"})
        except Exception:
            log.exception("record_tool_call failed")

    def record_llm_duration(self, model: str, duration_s: float) -> None:
        if not self._llm_duration:
            return
        try:
            self._llm_duration.record(duration_s, {A.GEN_AI_REQUEST_MODEL: model or "unknown"})
        except Exception:
            log.exception("record_llm_duration failed")

    def record_agent_duration(self, status: str, duration_s: float) -> None:
        if not self._agent_duration:
            return
        try:
            self._agent_duration.record(duration_s, {A.TWINKLE_AGENT_STATUS: status})
        except Exception:
            log.exception("record_agent_duration failed")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k metrics -v`
Expected: PASS（3 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/metrics.py tests/test_observability.py
git commit -m "obs: add Metrics fail-soft counters/histograms"
```

---

### Task 9: `instrumentors/llm.py` — 包 `LLMClient.stream` → `gen_ai.chat`

**Files:**
- Create: `twinkle/observability/instrumentors/__init__.py`（空占位）
- Create: `twinkle/observability/instrumentors/llm.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 建包占位**

Create `twinkle/observability/instrumentors/__init__.py`:
```python
"""agentserver instrumentors. apply_instrumentors lives here (Task 13)."""
```

- [ ] **Step 2: 写失败测试 — gen_ai.chat span + usage + TTFT + opt-in**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.agentserver.llm_client import TextDelta, Finish
from twinkle.observability.instrumentors.llm import instrument_llm


class _Cfg:
    def __init__(self, capture_messages=False):
        self.capture_messages = capture_messages


class _FakeLLM:
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
    assert "gen_ai.input.messages" not in attrs  # capture off


def test_instrument_llm_optin_captures_messages(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_llm(tracer, metrics, _Cfg(capture_messages=True), llm_cls=_FakeLLM)

    async def run():
        return [e async for e in _FakeLLM().stream(messages=[{"role": "user", "content": "hi"}], tools=[])]

    asyncio.run(run())
    assert len(exp.spans) == 1
    assert "gen_ai.input.messages" in exp.spans[0].attributes
    assert "gen_ai.output.messages" in exp.spans[0].attributes
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k instrument_llm -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.instrumentors.llm`。

- [ ] **Step 4: 实现 `instrumentors/llm.py`**

Create `twinkle/observability/instrumentors/llm.py`:
```python
"""Instrument LLMClient.stream -> gen_ai.chat span."""
from __future__ import annotations

import json
import time

from opentelemetry.trace import Status, StatusCode

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.observability import attributes as A
from twinkle.observability.context import current_request_context, increment_llm_counter

_TRUNC_LIMIT = 4096


def _trunc(s: str) -> str:
    return s if len(s) <= _TRUNC_LIMIT else s[:_TRUNC_LIMIT] + "..."


def _stamp_ctx(span) -> None:
    ctx = current_request_context()
    if ctx is None:
        return
    if ctx.request_id is not None:
        span.set_attribute(A.TWINKLE_REQUEST_ID, ctx.request_id)
    if ctx.session_id is not None:
        span.set_attribute(A.TWINKLE_SESSION_ID, ctx.session_id)


def _record_usage_attrs(span, usage: dict | None) -> None:
    if not usage:
        return
    inp = usage.get("prompt_tokens") or usage.get("input_tokens")
    out = usage.get("completion_tokens") or usage.get("output_tokens")
    tot = usage.get("total_tokens")
    if inp is not None:
        span.set_attribute(A.GEN_AI_USAGE_INPUT_TOKENS, int(inp))
    if out is not None:
        span.set_attribute(A.GEN_AI_USAGE_OUTPUT_TOKENS, int(out))
    if tot is not None:
        span.set_attribute(A.GEN_AI_USAGE_TOTAL_TOKENS, int(tot))


def instrument_llm(tracer, metrics, cfg, *, llm_cls=None) -> bool:
    if llm_cls is None:
        from twinkle.agentserver.llm_client import LLMClient as llm_cls

    def factory(original):
        async def traced(self, messages, tools):
            increment_llm_counter()
            span = tracer.start_span(A.SPAN_GEN_AI_CHAT)
            _stamp_ctx(span)
            model = getattr(self, "_model", "unknown")
            span.set_attribute(A.GEN_AI_SYSTEM, "openai")
            span.set_attribute(A.GEN_AI_REQUEST_MODEL, model)
            span.set_attribute(A.GEN_AI_OPERATION_NAME, "chat")
            if cfg.capture_messages:
                try:
                    span.set_attribute(A.GEN_AI_INPUT_MESSAGES, _trunc(json.dumps(messages)))
                    if tools:
                        span.set_attribute(A.GEN_AI_TOOL_DEFINITIONS, _trunc(json.dumps(tools)))
                except Exception:
                    pass
            start = time.perf_counter()
            first_token_ts = None
            finish_reason = "stop"
            usage: dict | None = None
            try:
                async for ev in original(self, messages, tools):
                    if isinstance(ev, TextDelta):
                        if first_token_ts is None:
                            first_token_ts = time.perf_counter()
                    elif isinstance(ev, Finish):
                        finish_reason = ev.finish_reason
                        usage = ev.usage
                        if cfg.capture_messages:
                            try:
                                span.set_attribute(
                                    A.GEN_AI_OUTPUT_MESSAGES,
                                    _trunc(json.dumps([ev.assistant_message])),
                                )
                            except Exception:
                                pass
                    yield ev
                if first_token_ts is not None:
                    span.set_attribute(
                        A.GEN_AI_STREAMING_FIRST_TOKEN_MS, int((first_token_ts - start) * 1000)
                    )
                span.set_attribute(A.GEN_AI_RESPONSE_FINISH_REASON, finish_reason)
                _record_usage_attrs(span, usage)
                metrics.record_token_usage(usage, model)
                metrics.record_llm_duration(model, time.perf_counter() - start)
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                span.record_exception()
                raise
            finally:
                span.end()

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(llm_cls, "stream", factory)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k instrument_llm -v`
Expected: PASS（2 个）。

- [ ] **Step 6: Commit**

```bash
git add twinkle/observability/instrumentors/__init__.py twinkle/observability/instrumentors/llm.py tests/test_observability.py
git commit -m "obs: instrument LLMClient.stream -> gen_ai.chat"
```

---

### Task 10: `instrumentors/tool.py` — 包 `ToolManager.execute` → `gen_ai.tool`

**Files:**
- Create: `twinkle/observability/instrumentors/tool.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — name + error 前缀检测 + opt-in args/result**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.observability.instrumentors.tool import instrument_tool


class _FakeToolManager:
    async def execute(self, name, args):
        if name == "boom":
            return "[tool error] ValueError: bad arg"
        return "ok-result"


def test_instrument_tool_emits_gen_ai_tool_span(tracer_exporter, meter_metricreader):
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
    assert "gen_ai.tool.arguments" not in span.attributes  # capture off


def test_instrument_tool_marks_error_on_tool_error_prefix(tracer_exporter, meter_metricreader):
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


def test_instrument_tool_optin_captures_args_result(tracer_exporter, meter_metricreader):
    tracer, exp = tracer_exporter
    meter, _ = meter_metricreader
    metrics = Metrics(meter)
    instrument_tool(tracer, metrics, _Cfg(capture_messages=True), tool_cls=_FakeToolManager)

    async def run():
        return await _FakeToolManager().execute("web_fetch", {"url": "x"})

    asyncio.run(run())
    attrs = exp.spans[0].attributes
    assert "gen_ai.tool.arguments" in attrs
    assert "gen_ai.tool.result" in attrs
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k instrument_tool -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.instrumentors.tool`。

- [ ] **Step 3: 实现 `instrumentors/tool.py`**

Create `twinkle/observability/instrumentors/tool.py`:
```python
"""Instrument ToolManager.execute -> gen_ai.tool span."""
from __future__ import annotations

import json
import time

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.instrumentors.llm import _stamp_ctx, _trunc

_TRUNC_LIMIT = 4096


def instrument_tool(tracer, metrics, cfg, *, tool_cls=None) -> bool:
    if tool_cls is None:
        from twinkle.agentserver.tools.manager import ToolManager as tool_cls

    def factory(original):
        async def traced(self, name, args):
            span = tracer.start_span(A.SPAN_GEN_AI_TOOL)
            _stamp_ctx(span)
            span.set_attribute(A.GEN_AI_TOOL_NAME, name or "")
            if cfg.capture_messages:
                try:
                    span.set_attribute(A.GEN_AI_TOOL_ARGUMENTS, _trunc(json.dumps(args)))
                except Exception:
                    pass
            start = time.perf_counter()
            error = False
            try:
                result = await original(self, name, args)
                if isinstance(result, str) and result.startswith(A.TOOL_ERROR_PREFIX):
                    error = True
                span.set_attribute(A.GEN_AI_TOOL_ERROR, error)
                if cfg.capture_messages:
                    try:
                        span.set_attribute(A.GEN_AI_TOOL_RESULT, _trunc(str(result)))
                    except Exception:
                        pass
                return result
            except Exception:
                span.set_attribute(A.GEN_AI_TOOL_ERROR, True)
                span.set_status(Status(StatusCode.ERROR))
                span.record_exception()
                raise
            finally:
                metrics.record_tool_call(name, error, time.perf_counter() - start)
                span.end()

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(tool_cls, "execute", factory)
```

> `_stamp_ctx` 和 `_trunc` 复用 `instrumentors/llm.py`（DRY）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k instrument_tool -v`
Expected: PASS（3 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/instrumentors/tool.py tests/test_observability.py
git commit -m "obs: instrument ToolManager.execute -> gen_ai.tool"
```

### Task 11: `instrumentors/agent.py` — 包 `AgentLoop.run_stream` → `twinkle.agent.invoke`（root）

**Files:**
- Create: `twinkle/observability/instrumentors/agent.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — root span + request/session id + iterations + error status**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.observability.instrumentors.agent import instrument_agent


class _FakeEnvelope:
    def __init__(self, request_id="req-1", session_id="sess-1", params=None):
        self.request_id = request_id
        self.session_id = session_id
        self.params = params or {}


class _FakeAgent:
    async def run_stream(self, envelope):
        yield "frame-1"
        yield "frame-2"


class _BoomAgent:
    async def run_stream(self, envelope):
        yield "frame-1"
        raise RuntimeError("loop failed")


def test_instrument_agent_emits_invoke_span(tracer_exporter, meter_metricreader):
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
    assert span.status.is_ok is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k instrument_agent -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.instrumentors.agent`。

- [ ] **Step 3: 实现 `instrumentors/agent.py`**

Create `twinkle/observability/instrumentors/agent.py`:
```python
"""Instrument AgentLoop.run_stream -> twinkle.agent.invoke (root span).

Opens the root span as current so child gen_ai.chat / gen_ai.tool spans
(parent = current) attach under it. Stamps request_id/session_id onto the
ContextVar so child spans can pick them up via _stamp_ctx. Counts LLM calls
via _llm_call_counter to set twinkle.agent.iterations at span end.
"""
from __future__ import annotations

import time

from opentelemetry.trace import Status, StatusCode

from twinkle.observability import attributes as A
from twinkle.observability.context import (
    current_llm_counter,
    reset_llm_counter,
    set_request_context,
)


def instrument_agent(tracer, metrics, cfg, *, agent_cls=None) -> bool:
    if agent_cls is None:
        from twinkle.agentserver.agent_loop import AgentLoop as agent_cls

    def factory(original):
        async def traced(self, envelope):
            req_id = getattr(envelope, "request_id", None)
            sess_id = getattr(envelope, "session_id", None)
            start = time.perf_counter()
            with tracer.start_as_current_span(A.SPAN_AGENT_INVOKE) as span:
                rctx_tok = set_request_context(
                    request_id=req_id, session_id=sess_id, agent_name=type(self).__name__
                )
                ctr_tok = reset_llm_counter()
                span.set_attribute(A.TWINKLE_REQUEST_ID, req_id or "")
                span.set_attribute(A.TWINKLE_SESSION_ID, sess_id or "")
                status = "succeeded"
                try:
                    async for ev in original(self, envelope):
                        yield ev
                except Exception:
                    status = "failed"
                    span.set_attribute(A.TWINKLE_AGENT_STATUS, status)
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception()
                    raise
                finally:
                    try:
                        if status == "succeeded":
                            span.set_attribute(A.TWINKLE_AGENT_STATUS, "succeeded")
                        span.set_attribute(A.TWINKLE_AGENT_ITERATIONS, current_llm_counter())
                        metrics.record_agent_duration(status, time.perf_counter() - start)
                    except Exception:
                        pass
                    ctr_tok.reset()
                    rctx_tok.reset()

        return traced

    from twinkle.observability.wrap import patch_method

    return patch_method(agent_cls, "run_stream", factory)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k instrument_agent -v`
Expected: PASS（2 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/instrumentors/agent.py tests/test_observability.py
git commit -m "obs: instrument AgentLoop.run_stream -> twinkle.agent.invoke"
```

---

### Task 12: `provider.py` — `init_providers`（不设全局）

**Files:**
- Create: `twinkle/observability/provider.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — none 返回 (None,None)；console 返回非空**

在 `tests/test_observability.py` 末尾加：
```python
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
    with tracer.start_span("probe") as s:
        s.set_attribute("x", 1)
```

> console exporter 会把 span 打到 stderr，pytest 默认捕获，正常跑不显示。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py -k init_providers -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.observability.provider`。

- [ ] **Step 3: 实现 `provider.py`**

Create `twinkle/observability/provider.py`:
```python
"""init_providers — build TracerProvider + MeterProvider; OTLP gRPC/console/none.

Returns (tracer, meter); does NOT set global providers — instrumentors take
the tracer/meter as params, so tests stay free of global-provider pollution.
Fail-soft: any error -> log + that signal disabled.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability.provider")


def _is_insecure(endpoint: str) -> bool:
    # http:// -> plaintext gRPC (insecure=True); https:// -> TLS.
    return endpoint.lower().startswith("http://")


def init_providers(cfg):
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": cfg.service_name})
    tracer = _init_tracer(cfg, resource)
    meter = _init_meter(cfg, resource)
    return tracer, meter


def _init_tracer(cfg, resource):
    if cfg.traces_exporter == "none":
        return None
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        tp = TracerProvider(resource=resource)
        if cfg.traces_exporter == "console":
            tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif cfg.traces_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            tp.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=cfg.endpoint,
                        headers=cfg.headers or None,
                        insecure=_is_insecure(cfg.endpoint),
                    )
                )
            )
        return tp.get_tracer("twinkle")
    except Exception:
        log.exception("tracer provider init failed; traces disabled")
        return None


def _init_meter(cfg, resource):
    if cfg.metrics_exporter == "none":
        return None
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        readers = []
        if cfg.metrics_exporter == "console":
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

            readers.append(
                PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=3000)
            )
        elif cfg.metrics_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=cfg.endpoint,
                        headers=cfg.headers or None,
                        insecure=_is_insecure(cfg.endpoint),
                    ),
                    export_interval_millis=3000,
                )
            )
        mp = MeterProvider(metric_readers=readers, resource=resource)
        return mp.get_meter("twinkle")
    except Exception:
        log.exception("meter provider init failed; metrics disabled")
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py -k init_providers -v`
Expected: PASS（2 个）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/observability/provider.py tests/test_observability.py
git commit -m "obs: add init_providers (no global providers)"
```

---

### Task 13: `apply_instrumentors` + `setup()`

**Files:**
- Modify: `twinkle/observability/instrumentors/__init__.py`（填 apply_instrumentors）
- Modify: `twinkle/observability/__init__.py`（填 setup()）
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写失败测试 — setup() disabled 时 no-op + 不抛**

在 `tests/test_observability.py` 末尾加：
```python
from twinkle.observability import setup
from twinkle.observability.instrumentors import apply_instrumentors


def test_setup_noop_when_disabled(monkeypatch):
    for k in _OBS_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert setup() is False
    assert setup() is False  # still no-op, no raise, _APPLIED stays False
```

> 不测 `setup()` enabled=true 路径——那会 patch 真 `LLMClient`/`AgentLoop`/`ToolManager`，污染其它测试。enabled 路径由 Task 15 的集成测试（用 fake 类调 `apply_instrumentors`）+ 手动验收覆盖。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_observability.py::test_setup_noop_when_disabled -v`
Expected: FAIL — `ImportError: cannot import name 'setup'`。

- [ ] **Step 3: 实现 `apply_instrumentors`**

把 `twinkle/observability/instrumentors/__init__.py`（Task 9 建的占位）整体替换为：
```python
"""Apply all agentserver instrumentors.

Each instrumentor is applied in its own try/except so one failing surface
doesn't break the rest. Production passes *_cls=None (lazy import of the
real class); tests pass fakes.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability")


def apply_instrumentors(tracer, metrics, cfg, *, agent_cls=None, llm_cls=None, tool_cls=None):
    from twinkle.observability.instrumentors.agent import instrument_agent
    from twinkle.observability.instrumentors.llm import instrument_llm
    from twinkle.observability.instrumentors.tool import instrument_tool

    results = {}
    for label, fn in (
        ("agent", lambda: instrument_agent(tracer, metrics, cfg, agent_cls=agent_cls)),
        ("llm", lambda: instrument_llm(tracer, metrics, cfg, llm_cls=llm_cls)),
        ("tool", lambda: instrument_tool(tracer, metrics, cfg, tool_cls=tool_cls)),
    ):
        try:
            results[label] = fn()
        except Exception:
            log.exception("instrumentor %s failed", label)
            results[label] = False
    return results
```

- [ ] **Step 4: 实现 `setup()`**

把 `twinkle/observability/__init__.py`（Task 3 建的占位）整体替换为：
```python
"""twinkle.observability — agentserver observability (OTel + monkey-patch).

setup() is the single entry point: reads config, and if OTEL_ENABLED,
initializes OTel providers and monkey-patches the 3 agentserver choke
points (AgentLoop.run_stream / LLMClient.stream / ToolManager.execute).
Idempotent + fail-soft; OTEL_ENABLED=false (default) is a zero-cost no-op.
"""
from __future__ import annotations

import logging

log = logging.getLogger("twinkle.observability")

_APPLIED = False


def setup() -> bool:
    global _APPLIED
    if _APPLIED:
        return True
    try:
        from twinkle.observability.config import load_config

        cfg = load_config()
        if not cfg.enabled:
            return False
        try:
            from twinkle.observability.provider import init_providers
        except ImportError:
            log.warning(
                "opentelemetry not installed; observability disabled (pip install -e '.[obs]')"
            )
            return False
        tracer, meter = init_providers(cfg)
        if tracer is None:
            log.warning("observability enabled but traces disabled; instrumentation needs a tracer")
            return False
        from twinkle.observability.instrumentors import apply_instrumentors
        from twinkle.observability.metrics import Metrics

        metrics = Metrics(meter) if meter is not None else None
        apply_instrumentors(tracer, metrics, cfg)
        _APPLIED = True
        log.info(
            "twinkle observability applied (traces=%s metrics=%s)", True, meter is not None
        )
        return True
    except Exception:
        log.exception("twinkle observability setup failed; continuing without telemetry")
        return False
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_observability.py::test_setup_noop_when_disabled -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add twinkle/observability/instrumentors/__init__.py twinkle/observability/__init__.py tests/test_observability.py
git commit -m "obs: add apply_instrumentors + setup() entry point"
```

### Task 14: `__main__.py` 挂接 `setup()`

> 命名例外之二（§1 命门约束）：进程入口装配，非业务插桩。`import` 放 `if __name__` 块内，测试 import 本模块不触发 setup。

**Files:**
- Modify: `twinkle/agentserver/__main__.py`

- [ ] **Step 1: 改 `__main__.py`**

把：
```python
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(main())
```
改为（在 `asyncio.run(main())` 之前加两行）：
```python
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    import twinkle.observability
    twinkle.observability.setup()
    asyncio.run(main())
```

- [ ] **Step 2: 语法检查**

Run: `python -c "import ast; ast.parse(open('twinkle/agentserver/__main__.py', encoding='utf-8').read()); print('ok')"`
Expected: `ok`。

- [ ] **Step 3: 既有测试零回炉（OTEL_ENABLED 未设 → setup() no-op）**

Run: `python -m pytest tests/ -q`
Expected: 全绿（含既有 + obs 测试）。

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/__main__.py
git commit -m "obs: wire setup() in agentserver __main__"
```

---

### Task 15: 端到端集成测试 + 全量验收

**Files:**
- Test: `tests/test_observability.py`

- [ ] **Step 1: 写集成测试 — 完整 trace 树（agent.invoke → chat + tool）**

在 `tests/test_observability.py` 末尾加：
```python
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
```

- [ ] **Step 2: 跑集成测试确认通过**

Run: `python -m pytest tests/test_observability.py::test_full_trace_tree -v`
Expected: PASS。若 `agent_span.context.span_id` 访问器报错，改用 `agent_span.get_span_context().span_id`（OTel SDK 版本差异）。

- [ ] **Step 3: 全量测试套件**

Run: `python -m pytest tests/ -v`
Expected: 全绿（既有 echo/agent_loop/llm_client/tool/integration + 全部 obs 测试）。

- [ ] **Step 4: 手动端到端验收（对齐 spec §10）**

```bash
# 装带 obs
python -m pip install -e ".[obs]"
# 起后端 collector（远程 101.37.215.110:4317 已就绪则跳过）
# 起两进程（带 OTEL env）
OTEL_ENABLED=true OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=http://101.37.215.110:4317 \
OTEL_SERVICE_NAME=twinkle-agentserver python scripts/start_services.py
# 另一终端起前端：cd web && npm run dev
# 浏览器 http://localhost:5173 发一条会触发工具调用的消息
```
Expected: collector 侧看到一条 trace：`twinkle.agent.invoke` 根下挂 ≥2 个 `gen_ai.chat` + 1 个 `gen_ai.tool`，含 token usage / 时延 / TTFT。`OTEL_ENABLED` 不设时行为与改造前一致（零回炉）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_observability.py
git commit -m "obs: add full trace-tree integration test + manual acceptance"
```

---

## Plan Self-Review

**Spec coverage（逐节对照 `2026-07-21-observability-module-design.md`）：**
- §1 做（OTel traces+metrics/OTLP-gRPC/monkey-patch 3 点/信号/env 默认关/metrics）→ Tasks 1,6,8,9,10,11,12。✓
- §1 不做（gateway/跨进程/context-token/skill·subagent·compaction/model_context/CLI/logs/ws_handler 根 span）→ 均未实现，§11 已记。✓
- §1 命门约束（业务代码零插桩调用 + 两处命名例外）→ 仅 Task 2（`llm_client.Finish.usage`）+ Task 14（`__main__ setup`）碰业务侧文件；`agent_loop/server/tools/manager` 全靠 monkey-patch，无编辑。✓
- §2 span 树 → Tasks 11,15。✓
- §3 模块结构 → 所有列出的文件均在 Tasks 3–13 创建。✓
- §4 插桩点表 + call.id 不采 → Tasks 9,10,11；call.id 确实未采。✓
- §5 span/metric schema + tool error 前缀检测 → Tasks 8,9,10,11。✓
- §6 `llm_client.Finish.usage` → Task 2（精确对应）。✓
- §7 配置 + `.env.example` → Tasks 1,6。✓
- §8 deps(optional `[obs]`) + 启动挂接 → Tasks 1,14。✓
- §9 fail-soft + 幂等 → patch_method（Task 4）/ provider（Task 12）/ Metrics（Task 8）/ setup（Task 13）全 fail-soft；幂等靠 `_twinkle_wrapped` + `_APPLIED`。✓
- §10 测试 + 验收 → Tasks 3–13 单测 + Task 15 集成/全量/手动验收。✓
- §12 参考仓借鉴/砍 → plan 借 `patch_method`/`InstrumentorConfig`/`init_providers`/`CollectingSpanExporter`/`gen_ai.*` 命名；砍跨进程/token 归因/CLI/logs，对齐。✓

**Placeholder scan：** 无 TBD/TODO/"implement later"/"similar to Task N"；每个代码步都有完整代码。✓

**Type consistency：** `instrument_*(tracer, metrics, cfg, *, <cls>=None) -> bool`、`apply_instrumentors(tracer, metrics, cfg, *, agent_cls=, llm_cls=, tool_cls=)`、`Metrics(meter)`、`init_providers(cfg)->(tracer,meter)`、`load_config()`、`setup()`、`_Cfg(capture_messages)`、`_stamp_ctx`/`_trunc`（llm.py 定义、tool.py 复用）、context 函数签名跨任务一致。✓

**两处 plan 偏离 spec（更优，已在 File Structure 注明）：** (1) fixtures 进 `test_observability.py` 而非 root conftest（零回炉）；(2) `init_providers` 不设全局（无测试污染）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-21-observability-module.md`. 两种执行方式：

**1. Subagent-Driven（推荐）** — 每个 task 派一个新 subagent，task 间我 review，迭代快、上下文干净。

**2. Inline Execution** — 在当前会话里用 executing-plans 批量执行，带 checkpoint review。

选哪种？

