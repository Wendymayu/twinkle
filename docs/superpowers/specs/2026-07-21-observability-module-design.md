# 可观测模块设计（agentserver, OTel + monkey-patch）

> 日期：2026-07-21
> 范围：为 `twinkle/agentserver` 新增一个 in-tree 可观测模块，用 OpenTelemetry 采集 agent 运行时 traces + metrics，通过 OTLP/gRPC 导出到 env 配置的 collector；插桩用 monkey-patch 挂在 3 个 choke point，业务代码零插桩调用（两处命名例外见 §1 命门约束）。
> 参考：`D:\code\opensource\github\jiuwenswarm-instrumentor`（OTel auto-instrumentation pip 包，monkey-patch + OTLP + W3C 跨进程）。借鉴其 `patch_method` / `InstrumentorConfig` / `init_providers` / `CollectingSpanExporter` 测试 fixture / `gen_ai.*` 属性命名；砍掉其跨进程传播、context-token 归因、skill/subagent/compaction、model_context fetcher、CLI wrapper（参考仓自己判多数为 overkill）。

---

## 1. 目标与范围

### 做
- 新建 `twinkle/observability/`：in-tree、单独目录，OTel SDK 采 traces + metrics，OTLP/gRPC 导出到 env 配置的 collector。
- monkey-patch 3 个 agentserver choke point：`AgentLoop.run_stream`（root）/ `LLMClient.stream`（gen_ai.chat）/ `ToolManager.execute`（gen_ai.tool）。
- 采集信号：LLM 调用（model / finish_reason / token usage / 时延 / TTFT）、工具调用（name / 时延 / error）、agent loop（request/session id / 迭代数 / 最终 status / 总时延）。
- env 驱动配置，`OTEL_ENABLED=false` 默认 → 零成本 no-op。
- metrics：token usage counter、tool count counter、LLM/工具/agent 时延 histogram。

### 不做（明确砍）
- gateway 插桩 + W3C traceparent 跨进程传播（用户定：只采 agentserver）。trace 树是 agentserver 内部。
- context-token 归因（参考仓 §8 判 overkill；token 总数已采，分桶以后端算）。
- skill / subagent / context-compaction 插桩（twinkle 无这些子系统）。
- litellm model_context fetcher / utilization ratio（多一个网络+缓存，overkill）。
- CLI wrapper（`jiuwen-instrument` 式 runpy 启动器）——in-tree 用 `setup()` 一行即可。
- logs 桥接（v1 不做；以后加 `OTEL_LOGS_EXPORTER=otlp` + stdlib logging handler 即可，预留 §11）。
- `server.ws_handler` 的 `twinkle.request` 根 span（v1.1 再加；见 §11）。

### 命门约束
**业务代码零插桩调用**：`agent_loop.py` / `llm_client.py` / `tools/manager.py` / `server.py` / `session_store.py` 等业务文件里**不得出现任何 observability 调用**（`tracer.start_span` / `meter.record_*` / `observer.on_*` 等）。所有插桩由 `twinkle/observability/` 在启动时 monkey-patch 施加。依赖方向单向：`observability → agentserver`（observability import agentserver 的类来打补丁），agentserver **永不 import observability**。

两处命名例外（均非插桩调用）：
1. `llm_client.py` 加 `Finish.usage` 字段（§6，暴露已有数据，不是 span/meter 调用）。
2. `__main__.py` 加一行 `twinkle.observability.setup()`（§8，进程入口装配，对齐参考仓 `setup()` fallback）。

---

## 2. 架构与 span 树

```
请求到达 → server.handler → AgentLoop.run_stream（被 patch 包住）
                              ↓ 开 twinkle.agent.invoke 根 span（盖 request_id/session_id 到 ContextVar）
                              for step in loop:
                                  LLMClient.stream（被 patch 包住）→ 开 gen_ai.chat 子 span
                                      ├─ TextDelta 流式 → 记 TTFT
                                      └─ Finish(finish_reason, usage) → 记 tokens / finish_reason / 时延
                                  若 tool_calls:
                                      ToolManager.execute（被 patch 包住）→ 开 gen_ai.tool 子 span（name/时延/error）
                              span end → 设 twinkle.agent.iterations / status / 总时延
                              ↓ BatchSpanProcessor → OTLP/gRPC → collector(101.37.215.110:4317)
```

span 树（单次请求，含一次工具调用两轮）：

```
twinkle.agent.invoke  (root, INTERNAL)
├─ gen_ai.chat        (step 1, INTERNAL)
├─ gen_ai.tool         (INTERNAL)
└─ gen_ai.chat        (step 2, 工具结果回灌后)
```

每个 `gen_ai.chat` 子 span 天然就是一次 loop step——不单独搞 step span（对齐参考仓 agent.invoke → gen_ai.chat 的模型）。

---

## 3. 模块结构

```
twinkle/observability/
  __init__.py           # 公开 setup()（唯一对外入口；apply_instrumentors 在 instrumentors/__init__.py，顶层未 re-export）
  config.py             # ObservabilityConfig + load_config()（OTEL_* + TWINKLE_OBS_* env）
  provider.py            # init_providers()：TracerProvider + MeterProvider over Resource，OTLP gRPC/HTTP + console/none
  attributes.py          # span/metric 属性键常量（gen_ai.* semconv + twinkle.* 自定义）
  wrap.py                # patch_method(cls, name, factory)：幂等、fail-soft、带 __wrapped__
  context.py             # request 上下文 ContextVar（request_id/session_id）+ _llm_call_counter
  metrics.py             # Metrics 类：counters/histograms + fail-soft record_*
  usage.py               # read_usage_token：统一读 token，兼容 dict（测试 fake）与 pydantic CompletionUsage（真 SDK 无 .get）
  instrumentors/
    __init__.py           # apply_instrumentors(tracer, meter, cfg)
    agent.py              # instrument_agent：包 AgentLoop.run_stream → twinkle.agent.invoke
    llm.py                # instrument_llm：包 LLMClient.stream → gen_ai.chat
    tool.py               # instrument_tool：包 ToolManager.execute → gen_ai.tool
tests/
  test_observability.py  # sync + asyncio.run + inline fake + CollectingSpanExporter
  conftest.py            # 加 CollectingSpanExporter + exporter fixture（照搬参考仓 ~10 行）
```

分文件理由（对齐 Phase 2 spec 风格）：每件独立职责、可单独测；`wrap.py` / `config.py` / `provider.py` 是基础设施（不依赖 agentserver），`instrumentors/` 各 surface 一个文件（生产传 `None` 懒加载真类、测试传 fake 类）。

---

## 4. 插桩点（3 个 choke point）

| 业务方法 | 文件:行 | span | 信号 |
|---|---|---|---|
| `AgentLoop.run_stream` | agent_loop.py:35 | `twinkle.agent.invoke` (root) | `twinkle.request.id` / `twinkle.session.id`（从 envelope arg 取）、`twinkle.agent.iterations`（ContextVar 计数）、status、总时延 |
| `LLMClient.stream` | llm_client.py:41 | `gen_ai.chat` | `gen_ai.system` / `gen_ai.request.model` / `gen_ai.response.finish_reason`、`gen_ai.usage.{input,output,total}_tokens`、`gen_ai.streaming.first_token_ms`、时延、`gen_ai.input.messages` / `gen_ai.output.messages` |
| `ToolManager.execute` | manager.py:44 | `gen_ai.tool` | `gen_ai.tool.name`、status、时延、`gen_ai.tool.arguments` / `gen_ai.tool.result` |

> `gen_ai.tool.call.id` v1 不采：`ToolManager.execute(name, args)` 签名里没有 tool_call id（id 在 agent_loop 解析 tool_calls 时可见，但那不在 choke point 上）。trace 树里 gen_ai.tool 已是 gen_ai.chat 同 trace 子节点，关联不丢；要 call.id 得改 patch 到 agent_loop 层，v1 不做。

### patch 机制（照搬参考仓 `wrap.py`）
`patch_method(cls, name, factory)`：
- 查 `cls.<name>`，调 `factory(original)` 造替换（`async def traced(self, ...)` 或 async generator），`setattr` 回去。
- **幂等**：替换打标记 `_twinkle_wrapped`，已包则 skip。
- **fail-soft**：任何异常 → log + skip，**不抛进业务**。
- 设 `__wrapped__` 便于 introspection。

streaming 的 patch 是 async generator wrapper：`async def traced(self, messages, tools): span=start; try: async for ev in original(self, messages, tools): <记 TextDelta/Finish>; yield ev finally: span.end()`。TTFT = 第一个 TextDelta 的时间戳 − span start。

---

## 5. span / metric schema

### span 属性（`attributes.py` 常量化，对齐 OTel GenAI semconv）

| span | 属性 |
|---|---|
| `twinkle.agent.invoke` | `twinkle.request.id`、`twinkle.session.id`、`twinkle.agent.iterations`、`twinkle.agent.status`(succeeded/failed)、duration(histogram) |
| `gen_ai.chat` | `gen_ai.system`="openai"、`gen_ai.request.model`、`gen_ai.operation.name`="chat"、`gen_ai.response.finish_reason`、`gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`、`gen_ai.streaming.first_token_ms`；`gen_ai.input.messages`、`gen_ai.output.messages`、`gen_ai.tool.definitions`（JSON，长度上限截断） |
| `gen_ai.tool` | `gen_ai.tool.name`、`gen_ai.tool.error`(true/false)、`gen_ai.tool.arguments`、`gen_ai.tool.result`（截断） |

> **error 判定**：`ToolManager.execute` 内部已 catch 工具异常并返回 `[tool error] {Type}: {msg}` 字符串（manager.py:50，不抛出）。故 gen_ai.tool span 的 status 恒为 OK、`gen_ai.tool.error` 由 wrapper 检测返回串的 `[tool error]` 前缀来置 true——这是已知契约，非启发式猜测。`gen_ai.tool.count` metric 的 `error` 维度同理。

### metrics（`metrics.py`，全 fail-soft）

| 类型 | 名字 | 维度 |
|---|---|---|
| counter | `gen_ai.client.token.usage` | `gen_ai.token.type`=input/output（input/output 都缺而仅有 total 时额外记一条 `total`） |
| counter | `gen_ai.tool.count` | `gen_ai.tool.name`、`error`=true/false |
| histogram | `gen_ai.client.operation.duration` | `gen_ai.request.model` |
| histogram | `gen_ai.tool.duration` | `gen_ai.tool.name` |
| histogram | `twinkle.agent.duration` | `twinkle.agent.status` |

### ContextVar（`context.py`）
- `_request_context`：dict(session_id, request_id, agent_name)，agent wrap 设、子 wrap 读来盖属性。`set_request_context(...)` 返回 token，`reset()` 放 finally。
- `_llm_call_counter`：agent wrap 重置 0、llm wrap 递增；agent wrap 在 span end 读它设 `twinkle.agent.iterations`。

---

## 6. 唯一业务改动：`llm_client.py` 暴露 token usage

现状：`llm_client.py:63` 把 OpenAI 末尾的 usage-only chunk 丢弃（`if not chunk.choices: continue`），且 `stream_options` 未设。外部 patch `stream` 只能看到 `TextDelta` / `Finish`，**看不到 tokens**。

改动（最小、observability-agnostic——只是把已有数据暴露出来，不加任何插桩调用）：

1. `Finish` dataclass 加字段：
   ```python
   @dataclass
   class Finish:
       finish_reason: str
       assistant_message: dict
       usage: dict | None = None   # 来自末尾 usage-only chunk
   ```
2. `stream()` kwargs 加 `stream_options`（让 openai 可靠发 usage；dashscope 本来就发，无副作用）：
   ```python
   kwargs = {"model": self._model, "messages": messages, "stream": True,
             "stream_options": {"include_usage": True}}
   ```
3. 循环体**顶部、在 `if not chunk.choices: continue` 之前**，捞 `getattr(chunk, "usage", None)`，最后一个非空值留用（有些 provider 把 usage 挂在最后一个 content chunk，不只 empty-choices 那个）。
4. `yield Finish(..., usage=captured_usage)`。

> 这是"暴露数据"不是"插桩调用"——`Finish.usage` 任何消费者都能用。但它确实改了业务文件，故单列一节、验收时显式确认这处改动。对齐用户 §8(a) 决定。

> **鲁棒性**：`stream_options` 是标准 OpenAI 参数，twinkle 当前 provider 支持（`llm_client.py:60-62` 现有注释已提到 openai/dashscope 的 usage chunk）。若将来换到不支持的 provider，usage chunk 不发 → `Finish.usage=None`，observability 侧 fail-soft 降级（`gen_ai.usage.*` 缺省不记），业务不受影响。

---

## 7. 配置（`config.py`）

`ObservabilityConfig`（实现为普通类 + `load_config()` 从 env 读；未用 `@dataclass(frozen=True)`）：

| env | 默认 | 说明 |
|---|---|---|
| `OTEL_ENABLED` | `false` | false → `setup()` 直接 return，零成本 no-op |
| `OTEL_TRACES_EXPORTER` | `none` | `otlp` / `console` / `none` |
| `OTEL_METRICS_EXPORTER` | `none` | 同上 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | `grpc` / `http`（**当前仅 gRPC exporter 落地，设 `http` 被静默忽略**） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | 如 `http://101.37.215.110:4317` |
| `OTEL_EXPORTER_OTLP_HEADERS` | — | 逗号分隔 `k=v`，鉴权用 |
| `OTEL_SERVICE_NAME` | `twinkle-agentserver` | Resource.service.name |

用户给定的一组（写入 `.env.example` 并注释默认关）：
```
OTEL_ENABLED=true
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_ENDPOINT=http://101.37.215.110:4317
OTEL_SERVICE_NAME=twinkle-agentserver
```

---

## 8. 依赖与启动挂接

### 依赖（pyproject 加 optional extra，不进核心）
```toml
[project.optional-dependencies]
obs = [
  "opentelemetry-api",
  "opentelemetry-sdk",
  "opentelemetry-exporter-otlp-proto-grpc",   # gRPC；http 另装 -proto-http
]
```
`pip install -e ".[obs]"` 才装。不装也能跑——`setup()` 用 try-import 守卫，`opentelemetry` 不在则 log + 跳过（fail-soft）。

### 启动挂接（第二处命名例外：进程入口装配）
`twinkle/agentserver/__main__.py`，`main()` 之前加一行：
```python
import twinkle.observability
twinkle.observability.setup()
```
`setup()`：`load_config()` → 若 `OTEL_ENABLED=false` 直接 return；否则 try-import opentelemetry → `init_providers(cfg)` 造 tracer/meter → `apply_instrumentors(tracer, meter, cfg)` patch 3 个 surface。幂等（`_APPLIED` 标记）。全 try/except，失败 log + return False。

> `__main__.py` 这一行不算"业务代码插桩"——它是进程入口的装配，对齐参考仓的 `setup()` fallback 路径。要彻底零文件改动，可另起 `python -m twinkle.observability.run` 启动器（import+patch 再 runpy agentserver）——v1 不做，留 §11。

---

## 9. 错误处理与 fail-soft

- patch 失败（类/方法找不到、factory 抛错）→ log + skip 该 surface，其余继续。
- provider init 失败 → log + 跳过，不打补丁。
- span/metric 记录异常 → 吞掉，**永不冒泡进业务**。
- 原方法抛错 → 照常传播，只把 span status 设 ERROR、记 exception。**另有一条非异常失败路径**：`run_stream` 正常 yield 了终止错误帧（`response_kind=="e2a.error"` / `status=="failed"`，如撞 `max_steps`）后正常 return——wrapper 在 yield 循环内检测该帧也置 status=failed + span ERROR（否则会被误标 succeeded；b60a084 修复点）。
- 幂等：重复 `setup()` 不重复包（`_twinkle_wrapped` / `_APPLIED` 双保险）。
- `OTEL_ENABLED=false` → 零开销（不 patch、不构造 provider）。

---

## 10. 测试策略（贴 twinkle 现有风格 + 借参考仓 fixture）

- `tests/test_observability.py` 内置 `CollectingSpanExporter` + `tracer_exporter`/`meter_metricreader` fixture（~10 行，**不进 `conftest.py`**——零回炉：不装 `[obs]` 时既有测试零污染；`conftest.py` 仍只有 `port_factory`/`free_port`）；metrics 用 OTel SDK `InMemoryMetricReader` 收集后断 counter/histogram 值。
- instrumentor 函数签名 `instrument_llm(tracer, metrics, cfg, *, llm_cls=None)`（三个 instrumentor 统一带 `cfg` 位置参数）：生产传 `None` 懒加载真 `LLMClient`；测试传 fake 类（同方法签名）。断 `exporter.spans[0].attributes[...]`。**不调真 LLM、不联网、不起 host**。
- sync 测试 + `asyncio.run` + inline fake，对齐现有 `_ScriptedLLM` / `_FakeClient` 风格。
- 必测：
  - `OTEL_ENABLED=false` → `setup()` 不 patch、exporter 无 span。
  - fail-soft：fake 原方法抛 `RuntimeError` → 仍传播、span status=ERROR；recorder 内部抛错 → 不影响原方法。
  - 3 个 surface 各自：`instrument_agent` 出 `twinkle.agent.invoke` + 正确 request/session id + iterations；`instrument_llm` 出 `gen_ai.chat` + finish_reason + usage(来自 `Finish.usage`) + TTFT；`instrument_tool` 出 `gen_ai.tool` + name + error。
  - 幂等：`patch_method` 重复包不叠加；`setup()` 重复调用安全。
  - input/output/tool-definitions 永远记录（截断）：`gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.tool.definitions` / `gen_ai.tool.arguments` / `gen_ai.tool.result` 均在 span attrs 里（测试断言存在）。
- §6 改动回归：`test_llm_client.py` 加 `Finish.usage` 透传断言（fake client 末尾发 usage chunk → `Finish.usage` 非空）。

### 验收（对齐"agent 运行时可观测数据采集"）
- 起两进程 + 前端发一条消息（含一次工具调用）→ collector 侧能看到一条完整 trace：`twinkle.agent.invoke` 根下挂 ≥2 个 `gen_ai.chat` + 1 个 `gen_ai.tool`，token usage、时延、TTFT 齐。
- `OTEL_ENABLED=false` 时全链路既有测试零改动全绿（零回炉，对齐 Phase 2 验收哲学）。

---

## 11. 后续（明确推迟，不在 v1）

- `server.ws_handler` 包一层 → `twinkle.request` 根 span，捕 ws 层 envelope 解析/发送错误（v1 的 root 是 `twinkle.agent.invoke`，ws 层错误不在内）。
- logs 桥接：stdlib `logging` handler → OTel `LogRecord`，拿 log↔trace 关联（加 `OTEL_LOGS_EXPORTER=otlp`）。
- gateway 侧插桩 + W3C traceparent 跨进程传播（要端到端 trace 时再加）。
- `python -m twinkle.observability.run` 零文件改动启动器。
- context-token 分桶归因（若后端算不出来再加）。

---

## 12. 与参考仓 jiuwenswarm-instrumentor 的对照

| 借鉴 | 砍掉 | 理由 |
|---|---|---|
| `wrap.patch_method`（幂等/fail-soft） | — | keystone，照搬 |
| `config.InstrumentorConfig` + env 驱动默认关 | — | on/off + per-signal exporter |
| `provider.init_providers`（Tracer+Meter over Resource） | LoggerProvider/logs | v1 只 traces+metrics |
| `gen_ai.*` 属性命名（OTel GenAI semconv） | context-token 7 桶归因 | 总数已采，分桶后端算 |
| `context.py` ContextVar + counter + reset-in-finally | cross-process W3C `inject/extract` | agentserver-only，无跨进程 |
| LLM instrumentor 形状（usage/TTFT/streaming span） | memory-op prompt 嗅探、model_context fetcher | overkill |
| `CollectingSpanExporter` 测试 fixture + 注入目标类 | — | 测试可对 fake 断言 |
| — | skill/subagent/compaction instrumentor | twinkle 无此子系统 |
| — | CLI wrapper（runpy `jiuwen-instrument`） | in-tree `setup()` 一行够 |
| — | logs OTelLogHandler | v1 不做（§11） |

twinkle 是参考仓这套 OTel auto-instrumentation 的**最小子集 + 学习重写**：monkey-patch 机制 + provider/config/测试模式对齐，砍掉一切跨进程/企业级/token 分桶，贴 twinkle 单进程 agentserver 定位够用为止。
