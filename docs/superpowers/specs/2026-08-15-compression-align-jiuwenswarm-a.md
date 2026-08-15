# Context Compression 对齐 jiuwenswarm — A 档设计

- date: 2026-08-15
- status: draft（待用户复核）
- 调研依据：[docs/superpowers/research/2026-08-15-jiuwenswarm-compression-mechanism.md](../research/2026-08-15-jiuwenswarm-compression-mechanism.md)
- 上游 spec：[2026-07-23-phase3-context-compression-design.md](2026-07-23-phase3-context-compression-design.md)（Phase 3 原始，自创未对齐）

## 1. 背景与动机

Twinkle 是 jiuwenswarm 核心代理管线的 learning-focused 重实现，刻意逐模块对照。但 compression 是少数没对齐的：
- Phase 3 spec 是**自创设计**（滑窗 + LLM 摘要，字符 //3 估算，不写回 history），通篇未对照 jiuwenswarm
- `docs/architecture.md` §11 对照表给 e2a/gateway/server/agent_loop/tools/observability/permissions 都立行，**唯独没给 compression 立行**

本次目标：把 compression 对齐 jiuwenswarm 压缩机制骨干。选 **A 档**（最轻、零文件冲突、可与 memory 会话并行）。

## 2. 范围

### 做（A 档三块）
1. 两层无 LLM 前置压缩（MicroCompact + ToolResultBudget）+ `protect_latest`
2. GET 不照搬 ADD + `compress_messages` 内部前置 `precompress_messages`
3. 摘要 prompt 结构化（4 节，硬编码 + mode 开关 fallback）

### 不做（后置 / 其他档）
- processor 链架构重构 / ADD 路径 / 可变 session buffer（C 档）
- tiktoken 估算（B 档）
- SessionMemory 会话笔记（B 档）
- precompress 可观测 span（后置，靠现有 `do_compress` span 间接）
- ToolResultDedup（链B第三层，后置）

## 3. 块1：两层无 LLM 前置

### 3.1 MicroCompact（清同工具旧结果堆叠）

机制：同工具名下保留最近 `keep_recent_per_tool` 条原文，更旧的清成 `cleared_marker`。每步组窗（GET）从完整 messages 重算，history.json 不动。

| 参数 | 值 | 含义 |
|---|---|---|
| `trigger_threshold` | 5 | 该工具**可清条数 > trigger+keep_recent（=5+3=8）才触发**——即积 >8 条同工具结果才清。非"积5条" |
| `keep_recent_per_tool` | 3 | 每工具留最近 3 条原文，更旧清成 marker |
| `compactable_tool_names` | `[read_file, grep, glob, command_exec, web_fetch, web_search]` | 长结果+易堆叠工具 |
| `cleared_marker` | `[Old tool result content cleared]` | 照搬 jiuwen |

> jiuwenswarm 的密度分档（15/10/5）是 `config.yaml` 声明但 **SDK 未消费的死配置**，不照搬，用单一 `keep_recent_per_tool`。

### 3.2 ToolResultBudget（大 tool 结果→预览）

机制：单轮 tool 结果**总量**超 `tokens_threshold` 才触发；触发后挑单条 > `large_message_threshold` 的候选里**最大那条**，在"发 LLM 这份 msgs"里换成 `trim_size` 字符预览。**history.json 原文不动**（Twinkle 无损 history 是天然原文库）。

| 参数 | 值 | 含义 |
|---|---|---|
| `tokens_threshold` | 9000（Twinkle token char//3） | 单轮 tool 总量超此才触发 |
| `large_message_threshold` | 3000 | 单条超此才 eligible |
| `trim_size` | 3000（字符） | 预览留多少字符 |
| `protect_latest` | 1 | 最新 1 条 tool result 永不 offload |

阈值换算：jiuwenswarm `tokens_threshold`=15000 占其 full_compact（100000）15% → Twinkle full_compact=60000×15%≈9000；`large_message`=5000 占 5% → 3000。`trigger_threshold`（条数）/`trim_size`（字符）单位无关照搬。

> Twinkle **不照搬** jiuwenswarm 的 offload json 文件 + 专用 `reload_original_context_messages` 工具——后者源码 handler 未找到（半成品），实际靠 `read_file` 读 offload 文件曲线 reload。Twinkle 用 history.json + 按 `tool_call_id` 拉回，更干净，绕开半成品。

### 3.3 `protect_latest`（Twinkle 增量，关键）

源码反证：jiuwenswarm ToolResultBudget 纯粹按 token 大小 `sort(reverse=True)` 取最大 offload，`messages_to_keep: null` 明确"不保留最新消息尾"——即"刚 read_file 大文件、这轮要看却被落盘"在 jiuwenswarm 真会发生。它只靠 `tool_name_allowlist=[read_file]` 按**工具**粗糙缓解（只保护 read_file，其他工具大结果照样这轮看不到）。

Twinkle 改成按**时序**保护 `protect_latest=1`：最新一条 tool result 无论什么工具都永不 offload。底气：无损 history.json 是天然原文库，旧结果落盘后 reload 从 history 拉，天然没有"reload 回来又被再 offload"的循环矛盾。

### 3.4 三阈值/规则咬合 + 链B顺序

咬合顺序：① `tokens_threshold`（单轮）动不动手 → ② `large_message_threshold`（单条）筛候选 → ③ 挑候选最大落盘 → ④ `protect_latest` 最新豁免。

链B两层执行顺序（对齐 jiuwenswarm 源码）：`ToolResultBudget → MicroCompact`（先落盘大的，后清旧的）。

## 4. 块2：GET 不照搬 ADD + `compress_messages` 内部前置

### 4.1 保持 GET，不照搬 ADD（5 理由）

1. 契合 Twinkle "无损历史、有损视图" 刻意取舍（jiuwenswarm ADD 写回可变 buffer、原文移出不可逆，会破坏无损）
2. 不引入 jiuwenswarm 可变 session buffer（`SessionModelContext`）——那是 C 档
3. offload 在 GET 下天然不需 offload 文件，history.json 是原文库
4. 每步重算和现状 `compress_messages` 一致（既有取舍，Phase3 已标"缓存优化后置"）
5. 和 memory 会话零冲突（GET 全在 compression 自己 hook/函数，不碰 server.py/SessionStore）

> 这是 A 档唯一"架构上偏离参考实现"的点。jiuwenswarm 两层都只走 ADD（源码确认未实现 GET 钩子）；Twinkle 刻意保持 GET。

### 4.2 调用链

```
现状：
ContextCompressionHook.before_model_call ─┐
ContextOverflowRecoveryHook (413)          ─┴─> compress_messages
        ├─ should_compress? --no--> return copy
        └─ do_compress     --yes-> head + [LLM 摘要 middle] + tail

A 档后：
ContextCompressionHook.before_model_call ─┐
ContextOverflowRecoveryHook (413)        ─┴─> compress_messages
        ├─ precompressed = precompress_messages(msgs)   ← 新，无 LLM
        │     ├─ ToolResultBudget: 大 tool 结果→预览（最新一条豁免）
        │     └─ MicroCompact: 同工具旧结果→marker（留最近 3 条）
        ├─ should_compress(precompressed)? --no--> return precompressed
        └─ do_compress(precompressed)     --yes-> head + [LLM 摘要 middle] + tail
```

关键收益：`precompress` 在 `should_compress` **之前**——先无 LLM 降 token，可能 precompress 完就低于阈值 → **省一次 LLM 摘要调用**。兑现 jiuwenswarm"成本递进、能省则省"逻辑。

### 4.3 单一入口覆盖两路径

主动 hook + 413 溢出恢复都调 `compress_messages`，自动享受两层前置（对齐 jiuwenswarm 两路径覆盖）。413 时最需前置（都溢出了，先无 LLM 清能省 LLM 摘要）。

## 5. 块3：摘要 prompt 结构化

### 5.1 为什么这样做（解决什么问题）

压缩的本质：被丢弃的 middle 压成一段摘要，agent 靠这段摘要重建工作上下文续作——**摘要质量直接决定续作质量**。

现状 `_summarize` 用自由文本 prompt（"把历史对话压成摘要，保留关键事实与工具结果"），LLM 自由发挥。痛点：自由摘要不强制覆盖**续作关键维度**，LLM 倾向写"做了什么"，漏"还差什么/踩过什么坑"：

1. **漏待办**：agent 做了 5 步只差最后跑测试，摘要只写"在实现功能"→ 续作时以为做完了，不再跑测试。
2. **漏错误与修复**：中间遇 import error 已修，摘要没提"曾遇 X 错误用 Y 修"→ 续作时重复踩坑或再试已失败路径。
3. **漏已用工具与文件**：已用 read_file/grep 摸清某文件结构，摘要只笼统"看了文件"→ 续作时重复读同一文件浪费。

核心：压缩后 agent 失去 middle 原文，靠摘要重建上下文；自由摘要不强制覆盖续作痛点维度，易漏。**结构化 prompt 在生成端逼 LLM 显式回答这几个维度**——而非在读端做定位/检索。4 节：关键事实与决定 / 已用工具与文件 / 待办与当前任务 / 错误与修复。

### 5.2 怎么做

- **硬编码 4 节 prompt 常量** `_STRUCTURED_SUMMARY_PROMPT`：要求 LLM 按固定 4 节标题输出。硬编码而非进 config，因这是带结构化输出契约的 prompt（强制按固定标题输出）——用户改坏（删节/改标题）→ 摘要漏维度 → agent 续作静默失败，符合 [[json-contract-prompts-not-in-config]]。
- **config 加 `context_compression.summary_prompt_mode`**（枚举 `structured` 默认 / `free`）：开关是简单枚举（选哪个模式），可进 config——用户能切回自由文本用自定义 prompt。
- **`free`** → 现状 `CONTEXT_SUMMARY_PROMPT`（自由文本，用户定制，向后兼容）。
- **`_summarize` 按 mode 选 system prompt**：`structured` 用硬编码常量，`free` 用 config 的 `CONTEXT_SUMMARY_PROMPT`。
- **4 节选型**（为啥这 4 节非 jiuwenswarm 9 节）：jiuwenswarm 9 节里 "All user messages"（逐条复述用户原话，middle 已含，冗余）、"Problem Solving"（问题解决叙事，不如"错误与修复"聚焦可复用结论）、"Primary Request"/"Optional Next Step" 可并入"待办与当前任务"。Twinkle 工具密集型 ReAct，续作最怕漏的是：还没做完啥（待办）、踩过啥坑怎么修（错误与修复）、摸清了哪些文件/工具（已用工具与文件）、定下哪些关键事实（关键事实与决定）。4 节覆盖续作痛点又不冗长。
- **实施时读 `config.yaml`** 确认现状 `summary_prompt` 值，设计 mode 默认行为。

### 5.3 trade-off

测试只能断"摘要含 4 节标题"（格式契约——LLM 输出里能找到 4 个固定标题字符串）；覆盖质量是否到位靠真模型观察，无法自动验证。不阻碍纳入：改动小、零冲突、防漏续作关键维度有实际价值。

## 6. 保留 Twinkle 取舍清单

- 无损 history.json：precompress 只改"发 LLM 这份 msgs"，原文不写回
- 字符 //3 估算：A 档不引 tiktoken
- MemoryFlushHook 不动：其"压缩前兜底写跨会话 LTM"是 Twinkle 自己的，不照搬 jiuwenswarm 的 SessionMemory 会话笔记
- `should_compress`/`split_messages_head_middle_tail`/`_render_messages_text` 签名不变（memory_flush_hook 依赖，不断）
- `compress_messages` 公开签名不变（加 precompress 是内部实现）

## 7. 文件边界（零冲突，可与 memory 会话并行）

| 动 | 不动 |
|---|---|
| [compression/__init__.py](../../../twinkle/agentserver/compression/__init__.py)（加 `precompress_messages` + 两层 + `protect_latest` + 结构化 prompt 常量） | [server.py](../../../twinkle/agentserver/server.py) hook 注册 |
| config.yaml/config.py 新增 `micro_compact.*`/`tool_result_budget.*`/`summary_prompt_mode` 键（新增≠改，不撞 memory 的键） | [instrumentors/compression.py](../../../twinkle/observability/instrumentors/compression.py)（现有 `do_compress` span 不动） |
| 测试 | MemoryFlushHook、`memory/*` |

**建议开 worktree**（干净 HEAD，隔离 memory 脏改动）实施。

## 8. 关键源码发现（支撑设计依据）

来自 `openjiuwen` SDK 源码调研（`.venv_dev_stable`）：
1. 两层都只走 **ADD** 路径（未实现 GET 钩子）→ Twinkle 刻意保持 GET（块4.1）
2. ToolResultBudget **不保护最新一条**（`messages_to_keep: null` 明确）→ `protect_latest` 是补洞（块3.3）
3. MicroCompact 触发门槛 = 可清条数 > `trigger + keep_recent`（非"积5条"）→ 块3.1 修正
4. 密度分档（15/10/5）SDK 未消费，死配置 → 不照搬（块3.1）
5. 链B顺序 `ToolResultBudget → MicroCompact → ToolResultDedup → FullCompact` → 块3.4 顺序；Dedup 后置
6. `reload_original_context_messages` 专用工具 handler 源码未找到（半成品）→ Twinkle 用 history.json 替代（块3.2）

**openclaw 第三参考**（`packages/agent-core/src/harness/compaction/compaction.ts`）：

7. 摘要**结构化 6 节** markdown（Goal / Constraints / Progress[Done/In Progress/Blocked 复选框] / Key Decisions / Next Steps / Critical Context）→ 印证块3 结构化方向是业界共识（jiuwenswarm 9 节 + openclaw 6 节，三参考全结构化）。
8. **retained-tail**：压缩产物追加进会话树，旧消息仍在磁盘被摘要"遮蔽"可回溯 → 印证 Twinkle"无损 history"取舍非自创。
9. **增量更新**（UPDATE_SUMMARIZATION_PROMPT：PRESERVE 旧 Goal / ADD 新 / In Progress→Done 迁移）→ 防中途追加目标永久漏；A 档后置（见 §11）。
10. **确定性文件抽取**（formatFileOperations 从 toolCall 参数抽 read/write 路径附摘要后）→ 不靠 LLM 记碰过哪些文件；后置。
11. openclaw 两段式（只保留尾，首部含最早用户消息被摘要）vs Twinkle 三段式（head 保留 system + middle 摘要 + tail）——差异源于 system 摆放（openclaw 运行时拼不在会话树；Twinkle 在 messages[0]），Twinkle head 保留对其架构必要。

## 9. 测试策略

- `precompress_messages` 单测：ToolResultBudget（大→预览 / 最新豁免 / allowlist）、MicroCompact（旧→marker / keep_recent / trigger 门槛）、`protect_latest` 边界
- `compress_messages` 集成：precompress → should_compress → do_compress 全链；**precompress 后低于阈值 → 省 LLM 摘要**（核心收益验证）
- 块3：摘要含 4 节标题（格式契约）
- 不破坏：[test_memory_flush_hook.py](../../../tests/test_memory_flush_hook.py) 仍过（签名不变）
- 无写回断言：原 msgs 不变
- 约定：无 pytest-asyncio，`asyncio.run()` + conftest fixtures；TDD（writing-plans 后先写测试）

## 10. 成功标准（可验证）

- precompress 后部分场景 `should_compress=false`（省 LLM 摘要）
- 最新一条 tool result 永不被 offload
- 摘要含 4 节标题
- 现有 compression / memory_flush 测试全过
- 和 memory 会话零文件冲突（git status 验证）

## 11. 后置项

- `twinkle.precompress` instrumentor span（靠现有 `do_compress` span 的 `before_tokens` 间接体现；细粒度后置）
- ToolResultDedup（链B第三层）
- tiktoken（B 档）
- SessionMemory 会话笔记（B 档）
- 增量更新（UPDATE_SUMMARIZATION_PROMPT，防中途追加目标永久漏；需引入摘要状态传递，偏离 GET 每步重算，属 C 档范畴）
- 确定性文件抽取（formatFileOperations，从 toolCall 参数抽 read/write 路径附摘要后，不靠 LLM）
- processor 链架构 / ADD / session buffer（C 档）
