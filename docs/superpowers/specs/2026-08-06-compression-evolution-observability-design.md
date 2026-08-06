# 上下文压缩 + Skill 进化可观测性埋点设计

## 背景

观测层（`twinkle/observability/`）目前只 instrument 了三个 choke point：`twinkle.agent.invoke`（根 span）、`gen_ai.chat`（LLM 调用）、`gen_ai.tool`（工具调用）。上下文压缩与 skill 进化是观测盲区：

- **压缩**：`ContextCompressionHook.before_model_call`（priority 95）委托 `compression.compress_messages`，超阈值时调 `_summarize`（一次 `llm.stream`）把 middle 摘成一条 system 消息。这次摘要 LLM 调用被 llm instrumentor 捕成 `gen_ai.chat` span，但和普通推理轮次无法区分——"压缩发生了吗"在 trace 里看不出来。
- **Skill 进化**：`SkillEvolutionHook.after_invoke`（priority 80）跑反馈环 + 进化扫描，委托 `OnlineEvolutionOrchestrator.run_feedback_loop` / `evolve`（各含 LLM 调用）。这些 LLM 调用同样被混在 `gen_ai.chat` 里分不出来；进化结果（status）完全不可见。skill 加载本身已通过 `list_skill`/`read_skill` 工具调用有 `gen_ai.tool` span，**不在本设计范围**。

## 目标 / 成功标准

为这两个子系统补 span 级可观测性，使其在 trace 中成为与工具调用对等的一等公民。

- 压缩发生时，trace 里出现一个 `twinkle.compression` span，且摘要那次 `gen_ai.chat` 嵌套为其子 span；未发生压缩时不产 span。
- skill 进化每次运行时，per-skill 出现 `twinkle.skill.evolution` span（带 `status`、`skill_name`）。
- 埋点逻辑全在 `twinkle/observability/instrumentors/`（monkey-patch，沿用现有 `patch_method` 幂等、fail-soft idiom）；业务侧仅 `compression/__init__.py` 内部拆分重构（`should_compress` + `do_compress`），公开 API 与两个 hook 零改动。
- 不采集 metric（压缩与进化均为低频事件，span 足矣；metric 维度从 span 可推导）。

## 非目标

- 不重构压缩算法 / 不做 Phase 5a 的协议化内存块或多级压缩。
- 不做 skill "版本进化追踪"（随时间 diff skill 内容）。
- 不动 skill 加载/热重载的埋点（已由工具调用覆盖）。

## 设计

### 1. 压缩 instrumentor

**前置重构**：把 `compress_messages` 拆成三个函数（都在 `compression/__init__.py`，纯内部重构，公开 API `compress_messages` 行为不变、两 hook 与现有测试不受影响）：

```python
def should_compress(msgs, *, token_threshold, keep_recent_pairs) -> bool:
    """两道闸都查：token 闸 + middle 闸。True 表示确实要压缩。"""
    if estimate_tokens(msgs) <= token_threshold:
        return False
    _head, middle, _tail = _split_keep_tool_pairs(msgs, tail_count=keep_recent_pairs * 2)
    return bool(middle)

async def do_compress(msgs, llm, *, keep_recent_pairs, summary_system_prompt) -> list[dict]:
    """真正执行压缩。假设 should_compress 已为 True（仍保留 `if not middle`
    兜底以防被直接调用）。含 _summarize 的 LLM 调用。"""
    head, middle, tail = _split_keep_tool_pairs(msgs, tail_count=keep_recent_pairs * 2)
    if not middle:
        return list(msgs)
    try:
        summary = await _summarize(llm, summary_system_prompt, _render_messages_text(middle))
    except Exception:
        return head + tail  # 降级：丢 middle、无摘要
    summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
    return head + [summary_msg] + tail

async def compress_messages(msgs, llm, *, token_threshold, keep_recent_pairs, summary_system_prompt) -> list[dict]:
    """薄壳：判定 → 委派。公开 API，行为与重构前完全一致。"""
    if not should_compress(msgs, token_threshold=token_threshold, keep_recent_pairs=keep_recent_pairs):
        return list(msgs)
    return await do_compress(msgs, llm, keep_recent_pairs=keep_recent_pairs, summary_system_prompt=summary_system_prompt)
```

文件：`twinkle/observability/instrumentors/compression.py`
函数：`instrument_compression(tracer, metrics, cfg, *, compression_mod=None) -> bool`

patch 目标：`do_compress`（patch 在 `twinkle.agentserver.compression` 模块上）。

`compression_mod=None` 时懒导入 `from twinkle.agentserver import compression as compression_mod`。

#### 为什么 patch `do_compress` 而非 `compress_messages` 或 hook 方法

三点同时成立：

1. **零假阳性**：`do_compress` 只在 `should_compress` 为真（两道闸都过）时才被调到，所以 wrapper 总是开 span 就对——不可能"开了 span 却没真压缩"。无需预测逻辑。
2. **零 hook 改动**：`do_compress` 是 `compress_messages` 的**同模块被调函数**，Python 调用时经模块 globals 解析名字（非 def 时绑定）。`setattr(compression模块, "do_compress", wrapped)` 直接到达 `compress_messages` 内部调用；两个 hook 照旧 `from ...import compress_messages`，**两条压缩路径（常规 + 溢出恢复）自动都覆盖**，无任何 hook 调用点改动。
3. **摘要 chat 嵌套**：wrapper 以 `start_as_current_span` 开 span 后调 original，`do_compress` 内 `_summarize` 的 `llm.stream` 产 `gen_ai.chat` 自然挂为本 span 子节点。

对比：patch `compress_messages`（跨模块 import 绑定，patch 到不了 hook 调用点，须改两 hook 调用方式）；patch `before_model_call`（只覆盖常规路径，漏溢出恢复）。patch `do_compress` 三者皆避。

代价：`should_compress` 查 middle 要 split 一次、`do_compress` 拿 head/middle/tail 再 split 一次——双 split，均廉价 list 切片，相对一次 LLM 摘要调用可忽略。

#### wrapper 逻辑（无预测，总是开 span）

`do_compress` 签名：`do_compress(msgs, llm, *, keep_recent_pairs, summary_system_prompt)`（不带 `token_threshold`——判定已由 `should_compress` 完成）。wrapper 同签名透传：

```python
def factory(original):
    async def traced(msgs, llm, *, keep_recent_pairs, summary_system_prompt):
        before_tokens = estimate_tokens(msgs)
        with tracer.start_as_current_span(A.SPAN_COMPRESSION) as span:
            _stamp_ctx(span)  # 拾取 agent instrumentor 设的 request_id/session_id ContextVar
            span.set_attribute(A.TWINKLE_COMPRESSION_TOKENS_BEFORE, before_tokens)
            try:
                result = await original(msgs, llm, keep_recent_pairs=keep_recent_pairs,
                                        summary_system_prompt=summary_system_prompt)
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
```

- `estimate_tokens` 从 `twinkle.agentserver.compression` import，与被 patch 的函数同源。
- `_stamp_ctx` 从 llm instrumentor 复用。`do_compress` 在 `ReActAgent.run` 内的 hook 里被（间接）调，agent instrumentor 已设好 ContextVar，子 span 能拾取。
- `_has_summary(msgs)`：检查返回消息里是否有 `content` 以 `[prior context summary]` 开头的 system 消息。区分"正常摘要压缩"与"`_summarize` 抛异常降级为 head+tail 无摘要"。

#### 异常语义

`do_compress` 内部已 catch `_summarize` 异常并降级为 head+tail（不抛），所以降级路径在 wrapper 看来是正常返回：`after_tokens < before_tokens`（middle 被丢）、`has_summary=False`、`compressed=True`（确实压缩了，只是无摘要）。wrapper 的 `except` 只在 `do_compress` 自身抛非预期异常（如 `_split_keep_tool_pairs` 出 bug）时触发——保持"异常上抛、record_exception、status=ERROR"的 idiom 与其他 instrumentor 一致。

### 2. 进化 instrumentor

文件：`twinkle/observability/instrumentors/evolution.py`
函数：`instrument_evolution(tracer, metrics, cfg, *, orchestrator_cls=None) -> bool`

patch 目标：`OnlineEvolutionOrchestrator.evolve`（per-skill，含 LLM 调用，返回带 `.status` 的 `EvolutionResult`）。

`orchestrator_cls=None` 时懒导入 `from twinkle.agentserver.evolution.orchestrator import OnlineEvolutionOrchestrator as orchestrator_cls`。

#### evolve wrapper

```python
def factory_evolve(original):
    async def traced(self, skill_name, conversation_messages, *args, **kwargs):
        with tracer.start_as_current_span(A.SPAN_SKILL_EVOLUTION) as span:
            _stamp_ctx(span)
            span.set_attribute(A.TWINKLE_SKILL_NAME, skill_name or "")
            try:
                result = await original(self, skill_name, conversation_messages, *args, **kwargs)
                if result is not None:
                    span.set_attribute(A.TWINKLE_EVOLUTION_STATUS,
                                       getattr(result, "status", "") or "")
                    span.set_attribute(A.TWINKLE_EVOLUTION_MESSAGE,
                                       _trunc(getattr(result, "message", "") or ""))
                return result
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.record_exception(exc)
                raise
    return traced
```

`evolve` 的签名以 `orchestrator.py:39` 为准（`evolve(self, skill_name, conversation_messages, ...)`），wrapper 用 `*args, **kwargs` 透传后续参数以适配签名演进。wrapper 以 `start_as_current_span` 开 span，使 `evolve` 内部 LLM 调用（信号检测、经验生成）的 `gen_ai.chat` 嵌套为本 span 子节点（与压缩同思路）。

#### 为什么只 patch `evolve` 不 patch `run_feedback_loop`

`run_feedback_loop` 返回 `None`（`orchestrator.py:157`，签名 `-> None`）——只做"LLM 评估 + 回写分数"，无 status。patch 它只能产一个带 `skill_name`、无 status 的 span，诊断价值低。`evolve` 是进化的主事件（生成经验、返回 `EvolutionResult.status` 如 `generated`/`no_signals`/`no_records`/`skipped_skill_not_found`），直接回答"哪个 skill 进化了、什么 status"——这是本设计目标。反馈环的 LLM 调用（`scorer.evaluate`）仍为不可区分的 `gen_ai.chat`，属现状、YAGNI；日后需要再加一行 patch 即可。

#### 为什么 patch orchestrator 而非 hook

备选 patch `SkillEvolutionHook._run_evolution` / `_run_feedback_loop`（hook 里已存在的私有方法）。但那是一 invoke 一个聚合 span，看不到 per-skill 的 status。orchestrator 的 `evolve(skill_name)` / `run_feedback_loop(skill_name)` 是 per-skill 且返回 status，直接回答"哪个 skill 进化了、什么 status、反馈环跑没跑"。代价是 instrumentor 耦合到 `evolution.orchestrator` 模块——与 tool instrumentor 耦合 `ToolManager` 同性质，可接受。

注意：`SkillEvolutionHook._run_evolution` / `_run_feedback_loop` 内部已 `try/except` 吞掉 orchestrator 异常（只 `log.exception`）。因此 evolution span 的 ERROR 只在 orchestrator 方法本身抛出且未被 hook 吞掉时触发——但因为 hook 在外层吞了，实际上 orchestrator 抛出的异常会被 hook 捕获，span 仍会先 `record_exception` 再上抛到 hook 的 except。嵌套关系：orchestrator span(ERROR, recorded) → 异常上抛 → hook 的 except 吞掉。span 里能看到失败记录，符合预期。

### 3. 属性常量

`twinkle/observability/attributes.py` 新增：

```python
# --- span names ---
SPAN_COMPRESSION = "twinkle.compression"
SPAN_SKILL_EVOLUTION = "twinkle.skill.evolution"

# --- twinkle.compression.* ---
TWINKLE_COMPRESSION_TOKENS_BEFORE = "twinkle.compression.tokens_before"
TWINKLE_COMPRESSION_TOKENS_AFTER = "twinkle.compression.tokens_after"
TWINKLE_COMPRESSION_COMPRESSED = "twinkle.compression.compressed"
TWINKLE_COMPRESSION_HAS_SUMMARY = "twinkle.compression.has_summary"
TWINKLE_COMPRESSION_STRATEGY = "twinkle.compression.strategy"

# --- twinkle.skill.* / twinkle.evolution.* ---
TWINKLE_SKILL_NAME = "twinkle.skill.name"
TWINKLE_EVOLUTION_STATUS = "twinkle.evolution.status"
TWINKLE_EVOLUTION_MESSAGE = "twinkle.evolution.message"
```

### 4. 注册接线

`twinkle/observability/instrumentors/__init__.py` 的 `apply_instrumentors` 在现有三元素列表后追加两条独立 try/except：

```python
("compression", lambda: instrument_compression(tracer, metrics, cfg, compression_mod=compression_mod)),
("evolution", lambda: instrument_evolution(tracer, metrics, cfg, orchestrator_cls=orchestrator_cls)),
```

并在函数签名加 `*, compression_mod=None, orchestrator_cls=None`（沿用现有 `agent_cls`/`llm_cls`/`tool_cls` 的测试可注入模式）。`metrics` 参数保留（签名一致）但两个新 instrumentor 内部不调用任何 `metrics.record_*`。

### 5. 测试

参考现有 `scripts/obs_smoke.py` 的 fake-openai + 内存 stub 模式。新增单测 `tests/test_observability_compression_evolution.py`：

- **压缩 no-op 不产 span**：消息量低于阈值或无 middle（`should_compress` 返回 False），断言无 `twinkle.compression` span（`do_compress` 根本没被调到）。
- **压缩发生产 span + 子 chat 嵌套**：构造超阈值且有 middle 的消息，fake openai 返回摘要，断言出现 `twinkle.compression` span、`tokens_after < tokens_before`、`has_summary=True`、且其下有一个 `gen_ai.chat` 子 span。
- **压缩降级路径**：fake openai 抛异常，断言 `compressed=True`、`has_summary=False`（`do_compress` 内部降级为 head+tail，middle 被丢但无摘要）。
- **两条调用路径都埋到**：分别经 `ContextCompressionHook.before_model_call` 与 `ContextOverflowRecoveryHook` 触发 `compress_messages`，断言两者都产 `twinkle.compression` span（patch 同模块 `do_compress` 自动覆盖两条路径）。
- **`compress_messages` 行为回归**：现有 `tests/test_context_compression.py` 全过（拆分后公开 API 行为不变）。
- **进化 evolve 产 span + status**：fake orchestrator 返回 `EvolutionResult(status="generated")`，断言 span 属性 `twinkle.skill.name`、`twinkle.evolution.status`。
- **幂等性**：`apply_instrumentors` 二次调用不重复 patch（`_twinkle_wrapped` 标记）。

## 文件清单

| 文件 | 改动 |
|---|---|
| `twinkle/observability/instrumentors/compression.py` | 新增——patch `compression` 模块的 `do_compress` |
| `twinkle/observability/instrumentors/evolution.py` | 新增——patch `OnlineEvolutionOrchestrator.evolve` / `run_feedback_loop` |
| `twinkle/observability/attributes.py` | 新增 span 名 + 属性 key 常量 |
| `twinkle/observability/instrumentors/__init__.py` | `apply_instrumentors` 注册两条新 instrumentor |
| `twinkle/agentserver/compression/__init__.py` | 重构：拆 `compress_messages` 成 `should_compress` + `do_compress` + 薄壳 `compress_messages`，行为不变 |
| `tests/test_observability_compression_evolution.py` | 新增单测 |
| `tests/test_context_compression.py` | 回归：现有用例应仍通过（`compress_messages` 行为不变） |

`evolution/orchestrator.py` 算法逻辑零改动；两个 hook（`context_compression_hook.py` / `context_overflow_recovery_hook.py`）零改动。

## 设计决策回顾

- **为什么拆 `compress_messages` 成 `should_compress` + `do_compress`**：拆分让 instrumentor patch `do_compress`（执行单元）而非 `compress_messages`（判定+委派）或 hook 方法。`do_compress` 只在确实要压缩时被调到 → wrapper 无预测、零假阳性；且 `do_compress` 是 `compress_messages` 的同模块被调函数（经模块 globals 解析，非 import 绑定），patch 它能到达两个 hook 的调用点 → 零 hook 改动、两条压缩路径（常规 + 溢出恢复）都覆盖。代价：双 split（廉价）。
- **为什么 patch `do_compress` 而非 `compress_messages` 或 hook 方法**：patch `compress_messages` 跨模块 import 绑定到不了 hook 调用点（须改两 hook）；patch `before_model_call` 只覆盖常规路径、漏溢出恢复、且裹了 hook 管道而非压缩本身。patch `do_compress` 三者皆避。
- **为什么 evolution patch orchestrator 而非 hook**：per-skill 粒度 + status 可见。hook 的 `_run_evolution` 聚合、无 status。
- **为什么不加 metric**：两子系统低频，span 已含 count/duration/status 可推导信息；metric 维度（counter/histogram）在低频下价值低，YAGNI。
