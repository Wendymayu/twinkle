# jiuwenswarm 上下文压缩机制调研

- date: 2026-08-15
- source: `D:\code\opensource\gitcode\jiuwenswarm`(包名 `jiuwenclaw`;压缩引擎在依赖 SDK `openjiuwen` 包 `.venv_*/Lib/site-packages/openjiuwen/core/context_engine/`)
- status: brainstorming 调研结论(对齐方案待定,见 §7)
- 关联:Twinkle compression 模块重构 brainstorming

## 0. 目的

Twinkle 想把 compression 模块对齐 jiuwenswarm。本文固化调研结论,供后续 spec/实施引用。

确认动机:Twinkle 的 compression 当初(Phase 3 spec `2026-07-23-phase3-context-compression-design.md`)是**自创设计**——选了"滑窗 + LLM 摘要 middle"方案 B,通篇未对照 jiuwenswarm,且显式标注刻意偏离(字符估算不引 tiktoken、不写回 history、每步重算)。`docs/architecture.md` §11 对照表给 e2a/gateway/server/agent_loop/tools/observability/permissions 都立了行,**唯独没给 compression 立行**——它是少数没对齐参考实现、也未登记的模块。

## 1. 全貌

压缩引擎**不在 jiuwenclaw 仓库**,在依赖 SDK `openjiuwen` 包。jiuwenclaw 自己只做两件事:(1) 薄 Rail 注入 offload 提示词(`JiuClawContextEngineeringRail`);(2) 413 被动恢复 Rail(`ContextOverflowRecoveryRail`)。

架构:**多 processor 链式**。`ContextEngine` 注册一串 processor,`SessionModelContext` 在两条路径上顺序触发每个 processor:
- **ADD 路径**(新消息入栈前):`trigger_add_messages` → `on_add_messages`
- **GET 路径**(组窗送 LLM 前):`trigger_get_context_window` → `on_get_context_window`

每个 processor 先跑廉价 `trigger_*` 判定要不要动手,返回 True 才跑贵的 `on_*`。两路径独立触发。

两条链(切链 = 改 `resources/config.yaml` 注释):
- **链B(默认,实配)** = SessionMemory + ToolResultBudget + MicroCompact + FullCompact
- **链A(备选)** = MessageSummaryOffloader + DialogueCompressor + CurrentRoundCompressor + RoundLevelCompressor

## 2. 链B四层(默认)

| 层 | 解决的垃圾形态 | 处理 | LLM | 损益 |
|---|---|---|---|---|
| MicroCompact | 同工具旧结果堆叠(8 次 read_file 的旧结果) | 同工具名保留每工具最近 N 条（单一 keep_recent_per_tool；密度分档 15/10/5 是 config 声明但 SDK 未消费的死配置，见 §10），旧的清成 `[Old tool result content cleared]` | 无(纯规则) | 标记,无所谓可逆 |
| ToolResultBudget | 单条 tool 结果巨大(一次读 2 万 token 大文件) | 单轮 tool 总量超阈值→最大条 **offload 落盘** json,上下文换 `<persisted-output>`+前 3000 字符预览,`reload_original_context_messages` 可拉回 | 无 | **无损**(原文在盘) |
| SessionMemory | "每次 LLM 摘要太贵且每步重算" | 后台异步把对话写成 `session_context.md` 笔记(9 节结构);FullCompact 压缩时**先看笔记**,笔记在且预算内→替换历史**跳过 LLM 摘要** | 有(后台异步) | 笔记是快照,可复用 |
| FullCompact | 前面都压不下,整体仍超 | 压缩边界切分:`head`=边界前(上次 summary + reinject 状态 skills/task_status/plan_mode 各 4000 字符)、`middle`=送 LLM 摘要(或用笔记)、`tail`=最近 10 条原文(保 tool_call↔tool_result 配对) | 有(兜底) | **真正有损**(丢细节保大意) |

加 **budget_guard**(非压缩,安全网):所有 processor 跑完仍超 `context_window_limit`→硬截断 head 20% + tail 80%,绝不发超大上下文给 LLM 导致 413。

## 3. 链A四层(备选,按对话结构粒度分)

| 层 | 粒度 | 处理 |
|---|---|---|
| MessageSummaryOffloader | 单条大消息 | 自适应压缩(extractive 抽原文关键句 / abstractive 重写摘要),返回 JSON 选策略;原文落盘 |
| DialogueCompressor | 完成的 user→assistant 块 | 每块→`[DIALOGUE_MEMORY_BLOCK]`,保留最近一轮 |
| CurrentRoundCompressor | 当前轮 span | →`[CURRENT_ROUND_MEMORY_BLOCK]`;二阶段合并 ≥3 旧块 |
| RoundLevelCompressor | 多轮递归 | L0→L1 递归合并,失败 aggressive pass(20000/10000),最终硬截断 head 20%+tail 80% |

## 4. 关键机制点

- **token 估算**:tiktoken 优先,字符 //3 兜底(`TiktokenCounter`,try 失败回退 `len(content)//3`)
- **摘要生成**:单独 LLM 调用,继承主 agent 默认模型(config 未配 processor 的 `model`→`bind_model_defaults` 继承 `models.default`)。FullCompact prompt = SDK 内置 `BASE_COMPACT_PROMPT`,要求 `<analysis>`+`<summary>` 双标签,9 节结构化(Primary Request / Key Technical Concepts / Files and Code Sections / Errors and fixes / Problem Solving / All user messages / Pending Tasks / Current Work / Optional Next Step)
- **413 恢复**(`ContextOverflowRecoveryRail`,priority=100,先于其他 rail):检测(关键词匹配)→强制 SessionMemory 更新→设 `force_compact`(下次 GET 无条件触发,`force=True` 接受任何缩减)→请求重试→熔断(连续 >3 次→写 `context_overflow_circuit_break` error 事件"建议 /compact 手动压缩或新会话")。成功后 `after_model_call` 重置计数
- **写回**:即时塑形为主(改内存 `_message_buffer`)+ 持久化(session_memory 笔记 / offload 文件)。**历史不可逆**——压缩后原文从内存移除,只能靠 `reload_original_context_messages` 工具从 offload 文件拉回
- **LTM 联动**:SessionMemory 是 FullCompact 的前置替代(笔记替代 LLM 摘要,省一次 LLM);413 时 force session memory 更新。**外部 LTM(KV+向量 chroma+sqlite,`ltm_search` 工具)不参与压缩流程**——是另一条并列 rail
- **配置**(jiuwenclaw `config.yaml` 实配覆盖):`full_compact.trigger_total_tokens=100000`、`session_memory.trigger_tokens=85000`/`trigger_add_tokens=80000`/`tool_min_=5`、`tool_result_budget.tokens_threshold=15000`/`large_message_threshold=5000`、`micro_compact.trigger_threshold=5`/`keep_recent_per_tool=10`、`context_window_limit_tokens=128000`(前端条)
- **可观测**:结构化日志(`[FullCompact]`/`[ContextOverflowRecovery]` 等前缀)+ Context Trace JSONL(env `OPENJIUWEN_CONTEXT_TRACE_ENABLED` 开关)+ `compression_usage` 统计(从 LLM response `usage_metadata` 提 input/output/total/cost/cache tokens)+ OTel 装饰器事件 + 前端上下文条

## 5. 为什么分层——四条设计逻辑

1. **成本递进**:规则(免费)→ 落盘(便宜,无损)→ 后台 LLM 笔记(贵但异步可复用)→ 同步 LLM 摘要(最贵有损)→ 硬截断(兜底)。贵的留最后,前面能省则省。Twinkle 现在只有"同步 LLM 摘要"一档,等于直接跳到最贵层。
2. **按"垃圾形态"分类,而非一刀切**:同工具旧结果堆叠 / 单条巨大 / 整体过长,各有最便宜处理。LLM 摘要解决不了"单条巨大该落盘保留"(摘要丢细节,落盘保留原文)。
3. **损益分级**:MicroCompact(标记,无所谓)/ ToolResultBudget(无损)/ SessionMemory(快照)/ FullCompact(有损)。只在必要时才走有损。Twinkle 的中段 LLM 摘要直接是有损,跳过所有无损预处理。
4. **关键路径 vs 后台**:同步只做必须的即时压缩(GET 时);SessionMemory 攒笔记挪后台异步,不阻塞 agent。笔记是"慢慢攒的资产",和"每次必做"的即时压缩解耦。

## 6. Twinkle vs jiuwenswarm 差异对照

| 维度 | Twinkle | jiuwenswarm | 性质 |
|---|---|---|---|
| 架构 | 单函数 `compress_messages`+两 hook | 8 processor 链 + ADD/GET 双路径 | 架构层级差 |
| 压缩层数 | 1 层(中段 LLM 摘要) | 4 层(2 无 LLM + 笔记 + LLM 兜底) | Twinkle 缺前两层无 LLM 主力 |
| token 估算 | 字符 //3 | tiktoken 优先 //3 兜底 | 缺 tiktoken(Phase3 标注"可接受") |
| 摘要 prompt | 自由中文模板 | 结构化 9 节 `<analysis>`+`<summary>` | 结构松散 |
| tool 配对闭合 | tail 起始落 tool 向左扩 | 同 | **已对齐** ✅ |
| 413 恢复 | 激进 keep_recent + 降 threshold → 重试 → 熔断 | force_session_memory → force_compact → 重试 → 熔断 | **机制同构** ✅(实现细节异) |
| 降级 | 摘要失败→丢 middle(head+tail) | FullCompact→20 条序列化 fallback | Twinkle 更激进 |
| 写回 | **不写回**(history.json 无损,只塑形) | 即时塑形 + 持久化笔记/offload,**历史不可逆** | 重大分歧,Twinkle 刻意取舍 |
| 压缩前兜底 | MemoryFlushHook(p96)写**跨会话** LTM(MEMORY.md/USER.md/daily) | SessionMemory 写**会话级**笔记(session_context.md,替代 LLM 摘要) | 思路近,目标不同 |
| 可观测 | instrumentor patch do_compress 产 span | 日志 + Context Trace JSONL + compression_usage + OTel 事件 + 前端条 | Twinkle 简单 |

## 7. 对齐建议(A/B/C,待用户选定)

| 档 | 做什么 | 与 memory 会话冲突? |
|---|---|---|
| **A(推荐)** | 补 MicroCompact + ToolResultBudget 两层无 LLM 前置 + 摘要 prompt 结构化对齐。**保留**单函数架构、无损 history、字符估算、MemoryFlushHook 不动 | **零冲突**(只动 compression 自己的文件) |
| **B** | A + 压缩前兜底对齐成 SessionMemory 会话级笔记模式(替代 LLM 摘要)+ token 估算换 tiktoken 优先 | 有(动 memory_flush_hook / 概念重叠,需协调) |
| **C** | B + `compress_messages` 单函数重构为 processor 链架构 + ADD/GET 双触发路径 + offload 文件机制 + 可观测对齐(Context Trace / compression_usage) | **必然冲突**(动 server.py / instrumentor / 两 hook,须等 memory 合并后) |

**推荐 A**:对齐 jiuwenswarm 真正省 token 的主力层(无 LLM 前置),保留 Twinkle 刻意取舍(无损 history),严格落在 compression 自己文件边界内,可与 memory 会话无冲突并行。

## 8. 链A vs 链B 选用判断(机制推断)

config 默认链B。推断依据(非 jiuwenswarm 团队明文理由):
- **链A**:细粒度、高频次 LLM 压缩(MessageSummaryOffloader 每条大消息调一次 LLM;DialogueCompressor 每块一次;CurrentRound 二阶段两次;RoundLevel 多级递归多次),一上来就是 LLM 语义压缩,**无"无 LLM 先清"的便宜层、不落盘保留原文**(进 memory block 即丢)。按对话结构粒度分。
- **链B**:成本递进,先无 LLM 清工具垃圾(MicroCompact)+ 落盘大 tool(ToolResultBudget,无损),再到后台笔记(SessionMemory)和 LLM 兜底(FullCompact)。便宜在前、贵在后,有无损层。按消息形态分。
- **默认链B的产品判断**:jiuwenswarm 主力场景是工具密集型 agent(代码/工具任务),对话里 token 大头是工具结果(文件内容/搜索结果/命令输出),链B 的 MicroCompact/ToolResultBudget 正好清这个,先无 LLM 把工具垃圾清掉成本最优。链A 按对话块/轮次粒度压缩,更适合**超长纯对话推理**(少工具、多轮深度思考),那时工具结果不是大头、对话块本身才是,链A 的块级 + 层级递归压缩更合适。
- **对 Twinkle 的启示**:Twinkle 是工具密集型 ReAct(17 工具),与 jiuwenswarm 主力场景一致 → 链B 更适合,A 档补链B 前两层而非链A 的东西。

## 9. 待定

- 对齐深度(A/B/C)用户尚未选定
- 链A vs 链B判断见 §8(机制推断,非明文)
- 最终 spec 待写(选定深度后写到 `docs/superpowers/specs/`)
- Twinkle 刻意取舍是否全部保留:无损 history(建议保留)、字符估算(可讨论是否换 tiktoken)

## 10. 源码复核修正（2026-08-15）

基于 `openjiuwen` SDK 源码复核，修正上文几处推断/描述（详见 spec §8 [2026-08-15-compression-align-jiuwenswarm-a](../specs/2026-08-15-compression-align-jiuwenswarm-a.md)）：

1. **密度分档 15/10/5 是死配置**（§2 已改）：`config.yaml` 声明 `density_profiles`/`adaptive_per_tool_budget`，但 SDK 源码未消费 → 不照搬，用单一 `keep_recent_per_tool`。
2. **链B实际含 5 层**（§2 标题"四层"欠精确）：`ToolResultBudget → MicroCompact → ToolResultDedup → SessionMemory → FullCompact`；Dedup 是第三层，A 档后置。
3. **两层都只走 ADD 路径**（§4"写回"暗含 GET 钩子）：SDK 未实现 GET 钩子，MicroCompact/ToolResultBudget 都在 ADD（入栈前）跑。Twinkle 刻意保持 GET（spec §4.1）。
4. **MicroCompact 触发门槛**：可清条数 > `trigger + keep_recent`（非"积 5 条"）；Twinkle trigger=5/keep=3 → 门槛 8（spec §3.1）。
5. **`reload_original_context_messages` 是半成品**（§4"写回"）：专用工具 handler 源码未找到，实际靠 `read_file` 读 offload 文件曲线 reload。Twinkle 用 history.json + `tool_call_id` 拉回替代（spec §3.2）。
6. **413 恢复的 force_compact** 对 FullCompact（GET）触发；ToolResultBudget/MicroCompact 不经 GET（spec §4.3 澄清）。
