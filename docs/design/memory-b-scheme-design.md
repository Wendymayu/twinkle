# 记忆架构方案 B 落地设计：写入简单，检索高效，后台整理

> 对应 B 方案「Daily Memory append-only log + Hybrid Search + Memory Flush + Dreaming/Consolidation」。本文是落地 spec：B 方案中 5a 已有的（Daily Memory 路径、Hybrid Search、`write_memory`/`memory_search`/`edit_memory`）不动，只补两个缺失组件——**Memory Flush**（压缩前兜底）与 **Dreaming**（后台整理）。Exact Dedup 不做（靠 Dreaming 语义去重覆盖）。

## 1. 背景与核心决策

### 1.1 为什么是 B 方案

5a 现状：写入靠模型调 `write_memory`（append 落盘 + `_index_file` 索引），检索靠 `memory_search`（hybrid 向量+FTS），冲突消解靠策略 prompt 教模型手动 `edit_memory`——**无自动整理**，矛盾/重复记忆会共存于文件，检索时多条都召回，是 agent 幻觉的祸根。

曾尝试的 5b（实时写入时 consolidation，after_invoke 对本轮新写入做 3 态判重/冲突）已判定为架构错误：局部判定（新 vs top-K）做不了 merge/抽取/价值过滤；把 LLM 成本/延迟/失败焊进写入关键路径；无工业先例；偏离 jiuwenswarm local 腿（local 腿靠后台 Dreaming，不是实时 `MemUpdateChecker`——那在 external 腿）。

B 方案对齐工业模式（WAL append-only log + index + 后台 compaction = LSM tree / Elasticsearch segment merge / Git gc）：**写入只记录（零 LLM）、检索只召回、压缩前兜底、后台整理**。LLM 成本与延迟隔离在 write 路径之外。

### 1.2 五个核心决策（已与用户确认）

1. **Dreaming 触发 = 后台 asyncio 定时 task**。AgentServer lifespan 启停，`interval` + busy-backoff（对话进行中跳过）。对齐 jiuwenswarm `DreamingOrchestrator` 的 `busy_checker` 思路。
2. **Spec 范围 = 全架构 + 两阶段实现**。Phase1 Flush → Phase2 Dreaming。Exact Dedup 不做。
3. **Dreaming 范围 = 只整理 `MEMORY.md`**。`daily_memory` append-only 不动（守 B §4 承诺），Dreaming 从 daily 抽长期事实追加到 `MEMORY.md`，`MEMORY.md` 内做去重/合并/冲突/删低值。
4. **Flush 接入 = 新 hook `MemoryFlushHook`**（priority 96，在 `ContextCompressionHook` 95 之前）。复用 `should_compress` 判定，只在要压缩时跑。
5. **Exact Dedup 不做**。靠 Dreaming 语义去重覆盖（含字面重复）。

### 1.3 关键约束（来自 gaps 核对）

jiuwenswarm 核对确认：其 `DreamingOrchestrator` 是**空壳定时器**（只做 busy-backoff 轮询，无反思/合并逻辑，无代码调用它整理记忆）。**B 方案 Dreaming 无现成参考，须自研**——B 方案的 hard precondition 成立。Phase 6 cron 框架是「到点起 agent 对话跑 `description`」模型，与 Dreaming「到点跑整理函数」模型不符，不复用。

## 2. 整体架构 — 职责边界与数据流

B 方案落地后四个职责清晰分离，写入路径零 LLM（守 B §4 承诺）：

| 职责 | 组件 | 何时跑 | 做什么 | 不做什么 |
|---|---|---|---|---|
| **写入** | `write_memory` tool + `MemoryManager.write` | 模型主动调 | append/overwrite 落盘 + `_index_file` 索引 | 不去重、不整理（5a 不动） |
| **检索** | `memory_search` tool + `MemoryManager.search` | 模型主动调 | hybrid 召回（5a 不动） | 不修改记忆 |
| **兜底** | `MemoryFlushHook`（新，priority 96） | 压缩前（`should_compress=true`） | LLM 查「即将丢弃的 middle」有无未持久化重要信息 → `write_memory` 落盘 | 不做语义抽取（非二次抽取，只兜底） |
| **整理** | `Dreaming`（新，后台 asyncio task） | 周期 + busy-backoff | `MEMORY.md` 语义去重/合并/冲突/删低值 + 从 daily 抽长期事实追加 | 不动 daily 原料 |

### 数据流

```
对话进行中 — 每步 before_model_call:
  ├─ MemoryFlushHook(96)  [新]
  │    └─ should_compress? 否 → no-op（无丢弃=无兜底必要）
  │    └─ 是 → LLM 查 middle 有无该记未记 → 有则 write_memory 落盘
  ├─ ContextCompressionHook(95) 压缩 middle（丢弃的已是兜底后的）
  ├─ SkillHook(90) 注入 skill 清单
  └─ MemoryHook(80) 注入策略 prompt + 被动召回

AgentServer 启动:
  └─ Dreaming asyncio task 起来（lifespan 挂启停）
       └─ await sleep(start_delay) → while running:
            await sleep(interval)
            if inflight_requests > 0: continue   # busy-backoff
            await _dream_once()
```

**关键边界**：Flush 在压缩**前**跑（priority 96 > 95），先把 middle 有价值信息落盘，再让压缩丢弃——丢弃时不丢信息。Dreaming 在**后台**跑，不碰 write/search 路径，不碰 daily。两者都 opt-in（config 默认关），关 = 维持 5a 行为零回归。

## 3. Phase 1 — MemoryFlushHook（压缩前兜底）

### 3.1 接入

新 hook `MemoryFlushHook`，`priority=96`（> `ContextCompressionHook` 95，先跑），`__init__(self, llm)` 构造注入（对齐 `ContextCompressionHook.__init__(self, llm, ...)`）。

### 3.2 触发逻辑（`before_model_call`）

1. lazy import `MEMORY_FLUSH_ENABLED`，关 → return（零回归）。
2. 复用 `compression.should_compress(msgs, token_threshold, keep_recent_pairs)` 判定：**false → return**（无压缩=无丢弃=无兜底必要，不是每步调 LLM）。
3. true → 从 compression 模块取 middle（复用 `_split_keep_tool_pairs` 逻辑，提取为 public helper 给 hook 层用），调 `_flush(middle)`。
4. fail-soft：任何异常 `log.exception` + 不崩（兜底是优化非承重，绝不阻断压缩/对话）。

### 3.3 兜底 ≠ 抽取

LLM 读 middle 全文（**含其中的 `write_memory` tool_call 历史**），输出「重要且**未被 `write_memory` 覆盖**的信息」——已写的排除，避免二次抽取。输出 JSON 数组，程序 `write_memory` 落盘（path 白名单由 `store.write` 把）：

```json
[{"path":"MEMORY.md","content":"...","append":true}]
```

无漏 → `[]`，不写。重复写入的善后交给后台 Dreaming（不做 Exact Dedup）。

### 3.4 LLM 调用

复用 `compression._summarize` 模板——`llm.stream(messages=[{system:flush_prompt},{user:middle_text}], tools=[])` + 收 `TextDelta`。prompt 可 config 覆盖。JSON 解析失败 → 不写 + log（fail-soft）。

### 3.5 Flush prompt 默认值

```
你是记忆兜底器。下面是即将被上下文压缩丢弃的对话中段（middle）。
检查其中有无【重要但尚未写进长期记忆】的信息：用户偏好/决策/持久事实/当日事件。
判定规则：
- middle 里的 write_memory 调用已把信息写进记忆的 → 不算漏，排除。
- 临时数据、当前任务过程性状态、寒暄、本轮就过期的事 → 不算漏。
- 已被覆盖的信息不要重复写。
有漏则输出要写的条目（JSON 数组），无漏则输出空数组 []。
只输出 JSON，禁止非 JSON 文本（不要代码块、不要解释）：
[{"path":"MEMORY.md|USER.md|daily_memory/YYYY-MM-DD.md","content":"要写的内容","append":true}]
path 必须是 USER.md / MEMORY.md / daily_memory/YYYY-MM-DD.md 之一。
```

### 3.6 三入口注册（对齐 MemoryHook）

- `server.py` `create_agent` auto-wire `MemoryFlushHook(llm=llm)`（和 `ContextCompressionHook` 同型，在它之后加）。
- `executor.py` `_hook_list` 加 `MemoryFlushHook(llm=self._llm)`。
- `manager.py` `_build_member` 加 `MemoryFlushHook(llm=self._llm)`。

子/team agent 上下文小不压缩时 `should_compress=false` 自动 no-op，无害。

### 3.7 OTel

新 span `twinkle.memory.flush`，属性 `new_writes`/`errors`，嵌在 `twinkle.agent.invoke` 下（对齐 compression instrumentor 风格，observability 关时 no-op `NonRecordingTracer`）。

### 3.8 compression 模块改动

`_split_keep_tool_pairs` 现为模块私有函数。提取一个 public helper（如 `split_messages_head_middle_tail(msgs, tail_count) -> (head, middle, tail)` 或 `render_middle(msgs, keep_recent_pairs) -> str`）供 `MemoryFlushHook` 取 middle。现有 `compress_messages`/`do_compress` 改调 public helper，行为不变。

## 4. Phase 2 — Dreaming（后台整理）

### 4.1 触发机制

AgentServer lifespan 启动时 `asyncio.create_task(_dreaming_loop())`，关闭时 cancel。loop 逻辑：

```
await asyncio.sleep(start_delay_seconds)   # 启动后延迟首跑，避免启动即整理
while running:
    await asyncio.sleep(interval_seconds)
    if inflight_requests > 0: continue    # busy-backoff：对话进行中跳过
    try:
        await _dream_once()
    except Exception:
        log.exception    # fail-soft，等下个 interval
```

`inflight_requests` = AgentServer 新增的活跃请求计数（`handle_message` 进来 +1 / 完成 -1），是 jiuwenswarm `DreamingOrchestrator.busy_checker` 的等价。需在 AgentServer 请求处理路径加计数。

### 4.2 整理算法 — B 模型（claimHash 晋升 + LLM 删行整合）

> 2026-08-17 重做：旧「方案 C」（N² pairwise `_dedupe_and_resolve` + 每段落 `_harvest_daily` LLM 抽取 + 判价值 + 3 个 prompt + OTel span）判定为「真的很烂」已全废。现模型参考 openclaw 的 daily→MEMORY.md promotion。完整设计见 [`dreaming-redesign.md`](dreaming-redesign.md)（§0 大白话、§6 dream body、§8 sidecar、§9 consolidate prompt、§10–§11 验证/compact、§14 方法增删），本文只折叠要点。

`dream()`（门卫 `enabled + llm + inflight==0` 过后）三步：

1. **晋升步 `_promote_append`（零 LLM，确定性）**：扫 `daily_memory/*.md` 非空行 → `claimHash = md5(line.strip())` 跨文件聚合 → 门 `len(source_files) ≥ min_distinct_files(默认2)` AND `hash ∉ state["promoted"]` → 逐条 `mgr.write("MEMORY.md", text, append=True)` + sidecar 记 `{ts,text,source_path}`。返回晋升条数 `count`；`count==0` → return（不跑 consolidate）。**幂等靠 sidecar `promoted` 集（只增不减，consolidate 删行不删 record）——不用 MEMORY.md marker**（marker 会随被 consolidate 删的行消失 → 下 tick 重晋）。

2. **整合步 `_consolidate`（一次 LLM）**：MEMORY.md 非空行编号 → LLM 出 `{"delete":[行号]}`（语义重复删冗余留更完整；矛盾删旧值留后写）→ 验证 `len(delete)/len(lines) ≤ max_delete_fraction(0.25)` + 行号 ∈ `[1,len]` + 非 bool → `mgr.replace` 原子写回（留存行逐字保留）。**LLM 全程不碰文本原文，只出行号**（先 append 后 delete，把"既加又合"降为"只删"）。任一步失败（LLM 异常/JSON 解析/验证不过）→ fail-soft 跳过，晋升步 append 的版本即为最终结果（等价 openclaw append-only fallback）。

3. **compact `_compact_if_over_budget`**：`len(MEMORY.md) > max_memory_chars(10000)` → 按 sidecar `ts` 升序丢最老的仍存在的 promotion 行（非 promotion 用户行不动）→ `mgr.replace` 写回。

### 4.3 数据结构

- **sidecar `<memory_dir>/dreaming_state.json`**（dreaming.py raw pathlib 自管，不走 `mgr.write` 白名单；原子写 tempfile+rename）：`{"version":1,"promoted":{"<hash>":{"ts","text","source_path"}}}`，`promoted` 只增不减。读 `_load_state`：不存在/坏 JSON/坏形状 → `{"version":1,"promoted":{}}`。写 `_save_state`：tempfile+rename，成功不留 `.tmp`。
- **claim**（`_scan_claims` 返回）：`{"<hash>":{"text":<strip 后行>,"source_files":set,"first_path":str}}`。文件内同行 = 1 claim；跨文件同 claim 累积 `source_files`。

### 4.4 LLM 调用（consolidate）

单次 consolidate prompt `_CONSOLIDATE_PROMPT` **硬编码进 `dreaming.py` 模块常量**（JSON 契约 `{"delete":[...]}`，不进 config，守 [[json-contract-prompts-not-in-config]]）。复用 `_ask_llm(prompt)` 收 TextDelta（异常 fail-soft 返回空串）。旧 3 prompt（pairwise `_JUDGE_PROMPT` + harvest `_EXTRACT_PROMPT` + 判价值）全删。

### 4.5 OTel

无。Dreaming 可观测已删（旧 `instrument_memory_dreaming` + `SPAN_MEMORY_DREAMING` + 6 属性焊进 `dream` 返回契约，违 [[observability-via-instrumentor-not-inline]]；`dream` 纯副作用返回 None）。以后需要再加走 instrumentor monkey-patch，不焊返回值。

## 5. config schema 汇总（新增两块，opt-in 默认关）

`config/schema.py`：
```python
class MemoryFlushConfig(_StrictModel):
    enabled: bool = False
    prompt: str = FLUSH_DEFAULT_PROMPT   # 默认值全文见 §3.5

class MemoryDreamingConfig(_StrictModel):
    enabled: bool = False
    interval_seconds: int = 3600
    start_delay_seconds: int = 300
    top_k: int = 5
    prompt: str = DREAMING_DEFAULT_PROMPT   # 默认值全文见 §4.5

class MemoryConfig(...):
    # 现有 dir/embed_model/query/hybrid/chunking/cleanup/auto_inject 不动
    flush: MemoryFlushConfig = MemoryFlushConfig()
    dreaming: MemoryDreamingConfig = MemoryDreamingConfig()
```

`config/__init__.py` 加 `MEMORY_FLUSH_ENABLED`/`MEMORY_FLUSH_PROMPT` + `MEMORY_DREAMING_ENABLED`/`_INTERVAL_SECONDS`/`_START_DELAY_SECONDS`/`_TOP_K`/`_PROMPT`（`MEMORY_<SECTION>_<FIELD>` 模式）。

`resources/config.yaml` 加 `memory.flush` / `memory.dreaming` 两块（`enabled: false` + 注释）。Flush 复用现有 `CONTEXT_TOKEN_THRESHOLD`/`CONTEXT_KEEP_RECENT_PAIRS`（走 `should_compress`）。

## 6. 降级矩阵

| 条件 | Flush | Dreaming |
|---|---|---|
| 无 LLM（provider=None） | 不挂 / no-op | task 不起 |
| `should_compress=false` | no-op（不调 LLM） | — |
| LLM 失败 / 非 JSON | fail-soft 不写不崩 | fail-soft 跳过该条 |
| 无 memory 文件 | — | no-op |
| search / edit 失败 | — | log 跳过该条 |
| inflight_requests > 0 | — | 跳过本轮 |
| 整次 `_dream_once` 异常 | — | log + 等下个 interval |

任何一环失败都不让对话/压缩/整理崩——兜底与整理都是优化非承重。

## 7. 边界速查

| 边界 | 设计 |
|---|---|
| 写入路径 LLM | **零**（B §4 承诺：`write_memory` 不去重不整理，LLM 只在 flush/dreaming） |
| Flush ≠ 抽取 | 只兜底（看 middle 的 `write_memory` 历史排除已写），不二次抽取 |
| Dreaming ≠ daily | daily append-only 不动，只整理 `MEMORY.md` + 从 daily 抽长期事实 |
| Dreaming ≠ write/search | 独立后台 task，不碰 write/search 路径 |
| opt-in | `flush.enabled`/`dreaming.enabled` 默认关 = 5a 行为零回归 |
| 子/team agent | Flush 三入口挂（小上下文 `should_compress=false` 自动 no-op）；Dreaming 进程级单 task 不重复 |
| inflight 计数 | AgentServer 新增 `_inflight_requests`（`handle_message` +1 / 完成 -1），Dreaming busy-backoff 用 |
| 无 sqlite-vec / 无 jieba | 不影响 flush/dreaming（都用 `mgr.search` + LLM，5a 降级矩阵已覆盖检索腿） |

## 8. 实现顺序（TDD 分阶段）

### Phase 1 Flush

1. config（`schema`/`__init__`/`yaml`）→ unblock `flush.enabled` gate。
2. RED 测试 7 个（见 §9.1），确认失败原因对。
3. `MemoryFlushHook` 骨架（`__init__`/`before_model_call` config-gate + `should_compress` 判定 + fail-soft + span 骨架）→ 测试 ①②⑦ GREEN。
4. compression 模块提取 public helper（取 middle）→ 行为不变回归。
5. `_flush`（LLM 调用 + JSON 解析 + `write_memory`）→ 测试 ③④⑤⑥ GREEN。
6. span 属性接 `new_writes`/`errors` → 全 GREEN。
7. 三入口注册（`server`/`executor`/`manager`）→ 全套 GREEN + 回归。

### Phase 2 Dreaming

1. config（`dreaming` schema/`__init__`/`yaml`）→ unblock gate。
2. RED 测试 10 个（见 §9.2），确认失败原因对。
3. `_dreaming_loop` + lifespan 启停 + AgentServer `_inflight_requests` 计数 → 测试 ①②③⑩ GREEN。
4. `_dream_once` 骨架（读 `MEMORY.md` + 拆条 + 无文件 no-op + 降级）→ 测试 ④ GREEN。
5. 整理逻辑（`search` 聚类 + LLM 判定 + `edit_memory`）→ 测试 ⑤⑥ GREEN。
6. 抽取逻辑（daily `search` + LLM 抽 + `append`）→ 测试 ⑦⑧ GREEN。
7. LLM 失败 fail-soft → 测试 ⑨ GREEN。
8. span 属性 → 全 GREEN + 回归。

## 9. 测试清单

### 9.1 Phase 1 Flush（7 个，TDD）

| # | 测试 | 验什么 |
|---|---|---|
| 1 | `test_flush_disabled_is_noop` | config 关 → 不调 LLM 不改 store |
| 2 | `test_should_compress_false_skips` | 开但未达压缩阈值 → no-op 不调 LLM |
| 3 | `test_flush_empty_writes_nothing` | LLM 返回 `[]` → 不写 |
| 4 | `test_flush_writes_extracted_items` | LLM 返回 `[{path,content}]` → `write_memory` 落盘 |
| 5 | `test_flush_non_json_fails_soft` | LLM 非 JSON → 不写不崩 |
| 6 | `test_flush_write_failure_fails_soft` | `mgr.write` raise → 吞掉不崩 |
| 7 | `test_flush_no_llm_is_noop` | 无 LLM（provider=None）→ no-op |

### 9.2 Phase 2 Dreaming（10 个，TDD）

| # | 测试 | 验什么 |
|---|---|---|
| 1 | `test_dreaming_disabled_task_not_started` | config 关 → task 不起 |
| 2 | `test_dreaming_enabled_starts_task` | enabled + lifespan → task 起 |
| 3 | `test_dreaming_busy_skips` | `inflight>0` → 跳过本轮 |
| 4 | `test_dreaming_runs_when_idle` | 空闲 + 有 `MEMORY.md` → 跑整理 |
| 5 | `test_dreaming_dedup_redundant` | 语义重复 → edit 留旧删新 |
| 6 | `test_dreaming_resolve_conflict` | 冲突 → edit 旧条改新值 |
| 7 | `test_dreaming_extract_new_fact_from_daily` | daily 新事实（search 无高相似）→ append `MEMORY.md` |
| 8 | `test_dreaming_skip_existing_fact` | daily 事实 search 高相似 → 不重复 append |
| 9 | `test_dreaming_llm_failure_fails_soft` | LLM 失败 → 跳过该条不崩 |
| 10 | `test_dreaming_no_llm_task_not_started` | 无 LLM → task 不起 |

测试约定：无 `pytest-asyncio`，用 `asyncio.run()` + free_port/port_factory fixtures（`tests/conftest.py`）；mock `LLMClient`（`_FakeLLM` 返回固定 `TextDelta` JSON + `Finish`）；`_set_memory_manager` 单例替换；Dreaming 测试用可控的 `_dreaming_loop`（短 interval + 直接 await `_dream_once` 验整理逻辑，不真等 sleep）。

## 10. 与 jiuwenswarm / B 方案关系

- **B 方案对齐**：Daily Memory append-only（5a 已有 `daily_memory/YYYY-MM-DD.md`）+ Hybrid Search（5a 已有）+ Memory Flush（新）+ Dreaming（新）。Exact Dedup 不做。守「写入简单，检索高效，后台整理」。
- **jiuwenswarm 差异**：jiuwenswarm local 腿无自动整理（`DreamingOrchestrator` 空壳），Twinkle 的 Dreaming 是**自研落地**（gaps §2.5 已确认无现成参考）。Flush 对齐 jiuwenswarm local 腿「今日 daily 自动注入」的补全思路但更进一步（压缩前兜底，不只注入）。Twinkle 不做 watchdog/interval 重索引（5a 刻意裁剪，Dreaming 不依赖文件监听）。
- **5a 不动**：`MemoryManager`（store/fts/embeddings/__init__）、`MemoryHook`、4 个 `@tool`、`memory-system-design.md` 描述的写入/检索/索引/淘汰机制全部保留。Dreaming 复用 `mgr.read`/`list_files`/`search`/`edit`/`write` 现有 API，不改 store。

## 11. 源文件索引（改动点）

| 组件 | 文件 | 改动 |
|---|---|---|
| `MemoryFlushHook`（新） | `hooks/builtin/memory_flush_hook.py` | 新建 |
| Dreaming loop + `_dream_once`（新） | `memory/dreaming.py`（新） | 新建 |
| compression public helper | `compression/__init__.py` | 提取 `_split_keep_tool_pairs` 为 public |
| AgentServer lifespan + inflight 计数 | `agentserver/server.py` | 加 task 启停 + `_inflight_requests` |
| 三入口注册 | `server.py`/`executor.py`/`manager.py` | 挂 `MemoryFlushHook(llm)` |
| OTel 属性 | `observability/attributes.py` | 加 `SPAN_MEMORY_FLUSH`/`SPAN_MEMORY_DREAMING` + 属性 |
| config | `config/schema.py`/`__init__.py`/`resources/config.yaml` | 加 `MemoryFlushConfig`/`MemoryDreamingConfig` |
| 测试 | `tests/test_memory_flush_hook.py`（新）/`tests/test_dreaming.py`（新） | TDD |
