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

### 4.2 整理算法 — 方案 C（检索聚类 + LLM 合并）

3 方案对比（A 整文件重写 / B 逐条判定 / C 检索聚类+合并），选 **C**：

- 复用现有 `MemoryManager.search`（5a hybrid 已有，不重造轮子）。
- 结构化聚类对齐 B 方案 industrial pattern（segment merge：读→聚类→合并→写）。
- 比 A 抗幻觉强（LLM 处理小类非整文件）；比 B 成本低（聚类摊销 LLM 调用）。
- 从 daily 抽新事实用 search 判「是否已存在」自然落地不重复。

### 4.3 `_dream_once()` 流程

```
1. 读 MEMORY.md 全文 → 拆条目列表（按 markdown 列表项/段落）
2. MEMORY.md 内整理:
   for 目标条目 in MEMORY.md 条目:
     相似组 = mgr.search(目标条目内容, top_k)   # 召回 MEMORY.md 内其它相似条目
     相似组非空 → LLM 判(目标 vs 相似组关系)
       redundant   → edit_memory 删目标条目（留相似旧条）
       merge       → edit_memory 用合并后内容替换相似旧条 + 删目标条目
       conflicting → edit_memory 相似旧条改为目标值 + 删目标条目
       independent → 不动
3. 删低值: LLM 判每条价值, 低值 edit_memory 删
4. 从 daily 抽长期事实:
   for daily_file in daily_memory/*.md:
     for 段落 in daily_file:
       相似 = mgr.search(段落, top_k)      # 查 MEMORY.md 有无
       无高相似(新事实) → LLM 抽长期事实 → append MEMORY.md
5. (edit/append 已自动 _index_file 重索引)
```

注：步骤 2 Dreaming 读的是 `MEMORY.md` 现状，条目间互相 search 比较关系——「目标条目」是当前 for 循环处理的条目，「相似旧条目」是 search 召回的其它条目；冲突时留目标值（文件内出现更后者视为更新的事实）。`edit_memory` 的 `old_text` **由程序从 `mgr.search` 返回的 `text` 字段拿**（程序定位，不让 LLM 输出原文——LLM 输出原文易不匹配 `old_text in text` 校验），LLM 只输出 `action` + `merged_content`。`mgr.search` 返回 `{path, score, text, start_line, end_line}`，供程序定位条目。

### 4.4 LLM 调用

复用 `compression._summarize` 模板，3 类 prompt（整理判定 / 判价值 / 抽长期事实），均可 config 覆盖。JSON 输出，解析失败 fail-soft 跳过该条。

### 4.5 Dreaming prompt 默认值

整理判定（目标条目 vs 召回相似旧条目）：
```
你是记忆整理器。下面是【目标条目】和它召回的【MEMORY.md 内相似旧条目】。
判断关系并输出操作：
- redundant（语义重复，同事实不同措辞）→ 保留旧条，目标条删除。
- merge（可合并成更完整的一条）→ 输出合并后内容，替换旧条，目标条删除。
- conflicting（同实体单一取值属性不同取值，如旧"用Windows"目标"用Mac"）→ 保留目标值，旧条改为目标值，目标条删除。
- independent（不同实体/不同属性）→ 都保留，不动。
旧条原文由程序从召回结果定位，你只需输出 action 与（merge 时的）合并后内容。
只输出 JSON，禁止非 JSON 文本：
{"action":"redundant|merge|conflicting|independent","merged_content":"合并后内容(仅merge非空)"}
```

判价值：
```
你是记忆价值判定器。下面是【记忆条目】。判断是否值得长期保留：
- 用户偏好/决策/持久事实/项目约定/架构 → 保留。
- 已过时（事实已变，且已有更新条目覆盖）→ 删除。
- 临时/过程性/低信息量 → 删除。
只输出 JSON：{"keep":true|false,"reason":"简短理由"}
```

抽长期事实（从 daily 段落）：
```
你是长期事实抽取器。下面是【日记段落】和它召回的【MEMORY.md 已有相似条目】。
从日记段落抽取【值得长期保留的事实】（用户偏好/决策/持久事实/项目约定），排除临时/过程性/当日就过期的事。
若召回的相似条目已覆盖该事实 → 不抽取（已存在）。
只输出要追加到 MEMORY.md 的新事实（JSON 数组），无新事实则输出 []：
[{"content":"新事实条目"}]
```

### 4.6 OTel

span `twinkle.memory.dreaming`，属性 `merged`/`deduped`/`conflicts_resolved`/`extracted`/`deleted`/`errors`。

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
