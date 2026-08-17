# Dreaming 写入机制对比 — jiuwenswarm / openclaw / Twinkle B

> 本文档记录重做 dreaming 前，对 jiuwenswarm 与 openclaw 两个参考实现的**写入路径 + 提升门槛**所做的核对与对比，作为 Twinkle B 方案（hybrid）设计的依据。Q&A 取自设计审查阶段的对话，事实均经源码核对。
>
> **核对时间**：openclaw 核对于 2026-08-17（两次 Explore agent 追踪：写入路径 + 提升打分）；jiuwenswarm 核对于 2026-08-12 `.venv_dev_stable` 版本（见记忆笔记 `jiuwenswarm-ltm-design`）。两者都在演进，以下为 point-in-time 观察。
>
> 关联设计文档：`dreaming-redesign.md`（B 方案完整规范）。

---

## 背景：Twinkle B 是什么

Twinkle 正在重做 dreaming（`twinkle/agentserver/memory/dreaming.py`），目标是"从日常记忆整理内容到 MEMORY.md"，守 B-scheme 不变量（写入路径零 LLM，去重/整合在后台 dreaming 进程）。选定的 **B 方案 = hybrid**：

- **快道（直写）**：agent 调 `write_memory`，按信息性质路由——持久事实（决策/偏好）→ MEMORY.md 直接写入，即时常驻可见；临时/episodic → `daily_memory/YYYY-MM-DD.md`。
- **慢道（dreaming 提升）**：后台定时扫 daily 非空行 → claimHash 跨文件聚合去重 → 门槛筛 → append 到 MEMORY.md（sidecar `dreaming_state.json` 记已晋升 hash 防重）→ LLM 整合（对整个 MEMORY.md 给删除行号列表去重，验证删除比例 ≤25%，失败软删除）→ 容量预算 compact。

核对两个参考实现，是为了确认 B 的设计是否站得住、有没有漏掉的关键点。

---

## Q1：jiuwenswarm 和 openclaw 也是"write_memory 路由 + dreaming 提升"这个写 MEMORY.md 的机制吗？

**结论：路由语义三方同源（都来自 jiuwenswarm），但"内容怎么进 MEMORY.md"三家不同。Twinkle B 是 hybrid，两个参考各只有一条道。**

| | agent 直写 MEMORY.md（快道） | dreaming 提升 daily→MEMORY.md（慢道） |
|---|---|---|
| jiuwenswarm | ✅ `write_memory` 直写 | ❌ 空壳，没做 |
| openclaw | ❌ 硬封禁（agent 不能写 MEMORY.md） | ✅ 唯一写入者 |
| Twinkle（B） | ✅ `write_memory` 直写 | ✅ 后台提升 |

### jiuwenswarm

- **路由语义和 Twinkle 完全同构**（Twinkle 这套本就是照它抄的，项目定位就是 jiuwenswarm 的学习型重实现）：`USER.md`=用户档案 / `MEMORY.md`=长期记忆（决策/偏好/持久事实）/ `daily_memory/YYYY-MM-DD.md`=每日笔记 + "用户说记住这个"默认落点（episodic，设计上就是 dreaming 的蒸馏源）。白名单就这三个路径模式。
- **写入纯模型驱动**：agent 调 `@tool write_memory(path, content, append)`，三个路径都能写。和 Twinkle 的 `write_memory` 一比一。
- **关键差异在 dreaming**：`DreamingOrchestrator`（`openjiuwen/core/memory/dreaming/orchestrator.py:12`）**只是空壳定时器**——只接受 `sweep_fn`+`interval`+`busy_checker` 做 busy-backoff 轮询，不含任何反思/合并/提升逻辑，也没有代码调用它整理记忆。local 腿也没有对话后自动抽取（自动抽取只在 external 腿的 `after_invoke→sync_turn`）。
- **所以 jiuwenswarm 只有"直写"这条道在跑**——agent 觉得是持久事实就直写 MEMORY.md，daily 里堆的 episodic 笔记没人整理、提升不上来。设计意图上 daily 写着"是 dreaming 的蒸馏源"，但 jiuwenswarm 自己没把 dreaming 做出来。

> 一句话：**Twinkle 正在重做的 daily→MEMORY.md 提升，恰恰是 jiuwenswarm 只画了图纸、没动工的那块。**

### openclaw

- agent 暴露的记忆工具只有 `memory_search`（读）、`memory_get`（读）、`intent`（提醒，不写文件）——**没有 write_memory 工具**。
- agent 写 daily（`memory/YYYY-MM-DD.md`）走的是它**普通的 write 工具**，但只在"压缩前的 memory flush 维护轮"里写，且这轮里 write 被**硬限制**成 append-only + 单路径（写别的路径直接 throw，写 MEMORY.md 也被拒）。prompt 层还明说 MEMORY.md/DREAMS.md/SOUL.md/AGENTS.md 这轮只读。
- **MEMORY.md 的唯一写入者是 dreaming 的提升路径** `applyShortTermPromotions` → `writeMemoryContent`（原子写，tempfile+rename），由 cron/heartbeat 触发。全仓 grep 它的调用方，只有 dreaming 提升代码两处，没有别的地方写 MEMORY.md。
- 提升候选来自一个短期 recall store（JSON 攒证据），untrusted 的 daily 还会被 quarantine——外部来源的内容没法被洗进 MEMORY.md。
- **三层都把 agent 挡在 MEMORY.md 外**：prompt 层（只读 hint）、工具层（路径限制 + append-only）、代码层（只有提升 applier 调 writeMemoryContent）。

> 一句话：**openclaw 把 agent 挡在 MEMORY.md 外，让 dreaming 独占写 = MEMORY.md 纯由 dreaming 整理。** 和 Twinkle/jiuwenswarm 的"agent 能直写"是相反的写入模型。

---

## Q2：openclaw 也是"出现两次的才写到 MEMORY.md"吗？

**结论：不是。openclaw 的门槛是"3 道硬地板全部满足 + 6 信号加权分 ≥ 0.75"，"出现两次"差得远。**

### 三道硬地板（ranking 和 apply 各过一遍，double-gate，全过才晋升）

| 地板 | 默认值 | 实际门控 | 代码 |
|---|---|---|---|
| 信号总数 | `≥ 3` | `signalCount = recallCount + dailyCount + groundedCount` | `short-term-promotion.ts:145` |
| 多样性 | `≥ 3` | `max(uniqueQueries, recallDays.length)` | `short-term-promotion.ts:155` |
| 加权分 | `≥ 0.75` | 6 信号加权求和（见下） | `short-term-promotion.ts:185` |

> 注意第一个地板名字 `minRecallCount` 有误导性——它门的是**三类信号加总**（`signalCount`），不是单独 `recallCount`。所以"被召回 2 次"本身根本过不了第一道地板。

### 6 个信号 + 默认权重（加权求和，`short-term-promotion.ts:175-182`，权重在 `short-term-promotion-utils.ts:33-40`）

| 信号 | 权重 | 量的什么 |
|---|---|---|
| relevance 相关度 | 0.30 | 召回时的平均搜索分（`totalScore/signalCount`） |
| frequency 频次 | 0.24 | `log1p(signalCount)/log1p(10)` |
| diversity 多样性 | 0.15 | `max(uniqueQueries, recallDays数)/5` |
| recency 最近性 | 0.15 | 指数衰减，半衰期 14 天 |
| consolidation 固化 | 0.10 | recallDays 跨度+间距，或 `groundedCount/3` |
| conceptual 概念 | 0.06 | `conceptTags数/6` |

（另外有个 `phaseBoost` 做梦相位加成，不是 6 信号之一，求和后直接加，最大 light+0.06、rem+0.09。）

### 什么算一次 +1

- **recallCount**：agent 调 `memory_search` 命中短期记忆路径（daily `.md`）的结果时 +1（**不是** active-memory 注入 prompt 触发）。去重规则：同一天同一 query 多次命中默认重复 +1。
- **dailyCount**：dreaming 扫描 daily 文件摄取时 +1。daily 按 **markdown 标题分块**（不是逐行），每块 = 一个 claim 候选，**用 claimHash 聚合、故意不带文件路径**——所以同一条事实出现在 3 个 daily 文件会聚合成 1 条 claim，dailyCount=3、recallDays=3。
- **groundedCount**：REM 做梦扫描时累加。
- **recallDays**：每次记录合并当天日期，`recallDays.length` = 这条 claim 跨几个不同日历天被召回/摄取。

### claim 怎么从 daily 文件进 recall store

**不是逐行直接变 claim**。有两条路径：
- **搜索命中（recall，增量）**：agent 调 `memory_search`，结果里来自短期记忆路径的命中段变成/强化一个 store 条目，key 含 path+行号+claimHash。
- **做梦扫描摄取（daily，批量）**：dreaming 扫 `memory/` 下 daily `.md`，先去自动管理行，按 markdown 标题分块，太短的 chunk（<8 字符）丢弃，每个 chunk 变 claim 候选，score 固定 = 0.62，用 `memory:claim:{claimHash}` 作 key（**故意省略文件路径**，跨文件同事实聚合成 1 条）。

### 对应到 Twinkle B

- **对得上的**：B 的 `_scan_claims` 用 claimHash 把同一事实跨 daily 文件聚合成 1 条 claim（`source_files` 集合）——跟 openclaw 的 daily 摄取聚合**一模一样**（它也是故意 key 不带路径、按 hash 聚合）。这块 B 是忠实的。
- **B 是 openclaw 6 信号里的 1.5 个的降维替身**：B 的"≥2 个不同 daily 文件" ≈ openclaw 的 **diversity 地板**（`recallDays.length`）+ 信号总数，但阈值从 3 降到 2。openclaw 另外 4 个信号（relevance/recency/consolidation/conceptual）B **算不出来**——relevance 要 memory_search 的搜索分，recency 要召回时间戳，conceptual 要概念标签，Twinkle 都没有。所以 B 只能量"跨天复现"这一个维度，单地板是**诚实的降维**，不是偷懒。
- **哲学 gap**：openclaw 提升的是"**被用到**"的（recall 驱动，relevance 权重最重 0.30），B 提升的是"**被重复写**"的（write 频率）。Twinkle 要补上"被用到"这维，得给 `memory_search` 加查询日志（记录每次召回的 claimHash + 时间）——这是更大的功能，out of B 的"不要大动"范围，deferred。
- **可调旋钮**：B 的 `min_distinct_files` 默认 2，openclaw 的对应地板是 3。嫌 B 提升太勤/太噪可调到 3 贴近 openclaw 严度。倾向默认 2（Twinkle 信号本来就稀，门槛太高啥都提不上去）。

---

## Q3：你觉得 jiuwenswarm 写 MEMORY.md 还是 openclaw 的实现好？

**纯架构上 openclaw 更好；但对 Twinkle，正在做的 hybrid（B）才是对的——不是折中妥协，是各取所长。**

### 为什么 openclaw 架构上更好

- MEMORY.md 是每个 session 都注入的"常驻记忆"，噪声会**复利**——一个错/重复条目税每一轮到永远。openclaw 把它当精选面管：dreaming 独占写 + 召回门槛 + 整合去重，MEMORY.md 越用越干净。
- 它的提升信号是"**被用到**"（recall ≥3 + 加权分），比"agent 觉得重要就直写"信号强——"agent 查了 3 次"比"agent 写了 1 次"更能证明这条事实有用。
- 单写入者 → 整合重写无竞态。

### openclaw 的真实代价

- **重**。flush 维护轮 + 工具硬限制 + append-only wrapper + provenance 观察者 + recall store + 6 信号打分 + quarantine + phase 信号，一整套机器管"决定哪些 daily 事实该持久化"这件事，对学习项目是 overkill。
- **可见性延迟 + 遗漏风险**：一次性持久决策（"用户改喝无糖可乐"）要等 dreaming tick + 召回够 3 次才进 MEMORY.md；没召回够就只在 daily（可搜但不常驻注入），下个 session agent 没主动 memory_search 就隐身了。

### 为什么 jiuwenswarm 不够

- 简单、即时（agent 直写 MEMORY.md，下轮常驻可见）——对"明显持久"的事实（偏好/决策）这其实**更好**。
- 致命伤是**没有清理**：没 dreaming，MEMORY.md 只增不减、不去重，跑久膨胀/重复/噪声累积，恰恰坏了它常驻注入这个最该干净的面的价值。它的 DreamingOrchestrator 是空壳，这个退化是真实的。

### 为什么 hybrid（B）对 Twinkle 才是对的

- B 的整合步骤扫的是**整个 MEMORY.md**（agent 直写的行 + dreaming 提升的行都包括），LLM 给删除列表去重。所以 agent 直写的噪声**不是永久污染**——整合周期性清理两路来源。你拿到 openclaw 的"最终干净" + jiuwenswarm 的"即时可见"，只丢了 openclaw 的 recall 信号（Twinkle 没查询日志，**本来也拿不到**，不是设计取舍是数据约束）。
- 纯 openclaw 对 Twinkle 是不划算的大动：要重写 `memory_hook`/`memory_flush_hook` 写入路由 + 改 prompt，而即使切过去，**没查询日志也用不了 openclaw 最好的信号（recall）**，还得退回用 write 频率——那正是 B 现在干的。花大代价切过去却拿不到核心好处，不值。
- 纯 jiuwenswarm（不建 dreaming）当前实现已判过"真的很烂"，也想要清理通道。两纯边都不是 Twinkle 要的。

### 一个诚实的保留

hybrid 比纯 openclaw 多一个**窄竞态**：agent 直写和 dreaming 整合同写 MEMORY.md，整合"读快照→写回"之间 agent 若 append 了一行，写回会用旧快照覆盖、丢那行。靠 idle 门（inflight==0 才跑）+ fail-soft（整合崩了跳过保留 append 结果）+ 验证（删除比例 ≤25% 拦截大面积误删）兜着；MEMORY.md 在两次整合之间是"半干净"的（直写没立刻去重）。这是 hybrid 的代价，但比例于 Twinkle 的规模和"不要大动"的约束，可接受。若日后要 openclaw 级安全，路径是给 `memory_search` 加查询日志 → 解锁 recall 信号 → 再考虑单写入者模型。

> 一句话：**要做一个跑几千 session 的成熟产品，选 openclaw 的模型；但 Twinkle 是学习型、已有直写通道、没查询日志、约束"不要大动"——hybrid 是唯一既拿到清理又不砍即时性、又不超范围的解。**

---

## 证据来源（源码路径）

### openclaw（核对于 2026-08-17）

**写入路径**：
- `extensions/memory-core/index.ts` — 工具注册（仅 `memory_search`/`memory_get`/`intent`；无 write 工具）+ flushPlanResolver/promotion wiring
- `extensions/memory-core/src/flush-plan.ts` — flush plan：daily 路径目标、append-only/read-only hints、recordWriteProvenance
- `src/auto-reply/reply/agent-runner-memory.ts` — flush driver：`runMemoryFlushIfNeeded`，启维护 agent 轮、监 write 工具、记 provenance
- `src/agents/agent-tools.ts` — `MEMORY_FLUSH_ALLOWED_TOOL_NAMES = {"read","write"}`，wrap write
- `src/agents/agent-tools.read.ts` — `wrapToolMemoryFlushAppendOnlyWrite`：硬单路径 + append-only 强制
- `src/agents/memory-write-provenance.ts` — provenance 观察者（记录不阻拦）
- `extensions/memory-core/src/short-term-promotion-memory-write.ts` — `writeMemoryContent`（唯一原子 MEMORY.md 写入者）
- `extensions/memory-core/src/short-term-promotion-apply.ts` — `applyShortTermPromotions`：唯一调 writeMemoryContent 处；quarantine untrusted daily
- `extensions/memory-core/src/dreaming.ts` — `runShortTermDreamingPromotionIfTriggered`/`registerShortTermPromotionDreaming`：cron/heartbeat 触发

**提升打分**：
- `extensions/memory-core/src/short-term-promotion.ts:92-237` — `rankShortTermPromotionCandidates`（打分主函数）
- `extensions/memory-core/src/short-term-promotion.ts:32-52` — `calculateConsolidationComponent`
- `extensions/memory-core/src/short-term-promotion-utils.ts:33-40,515-549` — `DEFAULT_PROMOTION_WEIGHTS`/`normalizeWeights`/`calculateRecencyComponent`
- `extensions/memory-core/src/short-term-promotion-utils.ts:289-299` — `totalSignalCountForEntry`
- `extensions/memory-core/src/short-term-promotion-types.ts:10-12,24-31,86-93` — 默认门槛 + 类型
- `extensions/memory-core/src/short-term-promotion-record.ts:151-342` — `recordShortTermRecalls`（+1 逻辑）
- `extensions/memory-core/src/dreaming-phases.ts:194-274,770-818,900-935` — daily 摄取分块（`buildDailySnippetChunks` 按标题）+ 批量摄取
- `src/memory-host-sdk/dreaming.ts:46-50` — 默认门槛常量（minScore=0.75, minRecallCount=3, minUniqueQueries=3, maxAgeDays=30）

### jiuwenswarm（核对于 2026-08-12 `.venv_dev_stable`）

- `jiuwenclaw/agentserver/memory/manager.py:100` — MemoryIndexManager（schema/search/sync）
- `jiuwenclaw/agentserver/tools/memory_tools.py:654-658` — 6 个 @tool（search/index/get/write/edit/read）
- `openjiuwen/core/memory/dreaming/orchestrator.py:12` — **空壳** `DreamingOrchestrator`（只 busy-backoff 轮询）
- `jiuwenclaw/agentserver/memory/external_memory_rail.py:143-201,211-261` — external before_model prefetch + after_invoke sync_turn（自动抽取唯一所在）
- 路由语义：`docs/zh/记忆.md` L53–56 + `memory/internal.py:237/248`
