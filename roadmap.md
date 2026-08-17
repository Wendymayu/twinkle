# Twinkle — 个人助手 Roadmap

## 0. 定位与决策

- **性质**：学习型重写项目。参考并重写 [jiuwenswarm](D:/code/opensource/gitcode/jiuwenswarm) 的**核心 agent 链路**，不是 fork、不是通用 SaaS 套壳。
- **主架构**：保留 gateway ↔ agentserver 两进程 + 双向 WebSocket，对齐 jiuwenswarm 主架构，降低后续与参考实现的差距。理由是学习对照，不是 cargo-cult。
- **技术栈**：Python。
- **用户通道**：当前只 Web，channel 适配层预留（后续可接钉钉/飞书，扩展点已就绪，需要时参考即可）。

> **参考实现勘误**：jiuwenswarm 是一个 monorepo（git origin = `openJiuwen/jiuwenswarm`，已从 JiuwenClaw 更名为 JiuwenSwarm v0.2.0），路径 `D:\code\opensource\gitcode\jiuwenswarm`，内含 `jiuwenswarm/`（swarm 框架）、`jiuwenclaw/`（agent 应用层）、`jiuwenbox/`（部署）三包，外部依赖 `openjiuwen`。主分支源码仅 `.pyc`，`.py` 源码在 `enterprise_dev` 分支（`git show enterprise_dev:<path>` 读取）。

### 明确保留（核心，已落地）
- gateway↔agentserver 两进程 + 双向 ws
- agent loop 核心闭环（ReAct：think → 选工具 → 执行 → 结果回灌 → 再决策）
- 工具系统（四层 ToolManager + `@tool` + 26 个 builtin 工具：web_fetch/web_search/command_exec/file_ops(5)/todo(4)/skill(2)/memory(4)/cron(5)/subagent(1)/workflow(1)/team(1)）
- Hook 系统（11 个 builtin hook + `WorkflowContextHook`：Permission/Skill/Memory/Compression/Retry/Logging/SubagentContext/OverflowRecovery/RepeatDetection/SkillEvolution/TeamContext/Workflow）
- 短期记忆（SessionStore 多轮对话记录）
- 长期记忆（MemoryManager SQLite 混合检索 + 4 个 memory 工具）
- 长会话上下文压缩（ContextCompressionHook 自动压缩）
- 单 web channel（+ channel 扩展点）
- **OTel 遥测切面**（`observability/` 包：`setup()` 在 `agentserver/__main__` 启动时接入，monkey-patch `ReActAgent.run` / `LLMClient.stream` / `ToolManager.execute` 三个 choke point 造 span，OTLP gRPC / console / none 导出 + 指标；`OTEL_ENABLED` 默认 false 为零成本 no-op）

### 明确超出范围（学习型项目不做）
这些要么偏企业 SaaS、要么强依赖 `openjiuwen` 外部生态、要么与"单机学习型重写"定位不符。需要时参考对应模块实现，**不进当前路线**：

- **企业级多租户**：`tenant_agent_pool`（多租户 AgentManager 池化/LRU/workspace 隔离）、`sts_service`（加解密 stub）—— SaaS 多租户隔离，与单机学习定位不符。
- **分布式 swarm**：pyzmq transport + PostgreSQL 存储 + `remote_member_bootstrap` —— 依赖 openjiuwen 分布式 runtime，过重。
- **ACP 编辑器嵌入**：JSON-RPC over subprocess（Zed 风格 agent server 协议）—— 偏编辑器集成，小众。
- **完整 Observability 套件**：langfuse 导出 / prompt redaction / IdentityStore / W3C 跨 ws trace 传播 —— 基础遥测已落地（见"明确保留"），本项指全套，不照搬。
- **manifest catalog + provider 工厂全套**：`HarnessElementDescriptor` + `ConstructionInput` + seed 重建 —— jiuwenswarm 的声明式装配依赖反射/序列化边界，门槛高；**了解思想，不照搬实现**。
- **Swarmflow / symphony 编排、多模态工具门控**：依赖 openjiuwen 生态，不照搬。

### 明确推迟（需时再做，不阻塞主线）
- **多 channel 广度接入**（飞书/钉钉/企微/discord/...）：Channel 扩展点已就绪（见 `docs/architecture.md` §5.5，加一个 channel 不动核心），需要某通道时按接口实现 `BaseChannel`（`start/stop/send/is_allowed` + platform_adapter）即可，不单独立项。

---

## 现状速览

- **Phase 0–2 已落地**：两进程骨架、agent loop 闭环、四层工具系统（含 todo/command_exec/file_ops）。
- **Phase 3（长会话上下文压缩）已落地**：`ContextCompressionHook`（`before_model_call` 自动压缩）+ `compression/` 算法包（滑窗+LLM 摘要），已合并进主线。对应里程碑 M4 ✅。
- **Phase 4（工具权限 / 审批 + 命令安全）已落地**：`permissions/` 包 + `PermissionHook` + ASK 挂起/恢复 + JSONL 审计 + `TWINKLE_PERMISSIONS` opt-in。对应里程碑 M5 ✅。
- **Phase 5a（长期记忆）已落地**：`memory/` 包 + `MemoryHook` + 4 个 memory 工具 + SQLite 混合检索。对应里程碑 M6 ✅。
- **Phase 6（定时任务 cron）已落地**：`CronSchedulerService`（min-heap + croniter + 两阶段 wake→push）+ `CronJobStore` + 5 个 cron 工具 + gateway 集成。对应里程碑 M7 ✅。
- **Phase 7（Skill 系统）已落地**：`skills/` 包 + `SkillHook` + SkillNet 一键下载/安装。对应里程碑 M8 ✅。
- **Phase 8（子 Agent）已落地**：`spawn_subagent` + `SubagentExecutor` + `SubagentContextHook` + ContextVar 隔离 + 软硬超时 + 递归保护。对应里程碑 M9 ✅。
- **Phase 8a（Todo 增强）已落地**：TodoTask 数据模型增强（id、blocked_by、owner、metadata）+ 4 个工具重写 + `sequential=True` + 前端状态分组。对应里程碑 M9a ✅。
- **OTel 遥测已落地**（`observability/` 包，启动接入，默认 off 零成本）：对应里程碑 M12 ✅。
- **Phase 9（溢出恢复 + 循环检测）已落地**：`ContextOverflowRecoveryHook`（413 自动压缩重试 + 熔断）+ `RepeatToolCallDetectorHook`（滑动窗口 + stable hash 循环检测 + 纠偏注入）。对应里程碑 M13 ✅。
- **Phase 10（HITL 中断/恢复）已落地**：`ApprovalPendingRecord` + `ApprovalRegistry.save_pending/clear_pending/get_pending` + `approval.check_pending` RPC + 前端重连恢复审批卡片。对应里程碑 M14 ✅。
- **Phase 11（Workflow 引擎 = PlanNode 递归执行树）已落地**：`workflow/` 包（`PlanNode` ABC + `WorkflowExecutor` + `_SAFE_BUILTINS` + `PlanCodeValidator` + `_fallback_wrapper`→SubagentExecutor + HookInterrupt 透传）+ `execute_workflow` 工具 + `WorkflowContextHook` + 内置 `pptx-craft` 7 节点流水线 + seed 机制。对应里程碑 M15 ✅。
- **Phase 14（Skill 自进化 v1）已落地**：`evolution/` 包（`ConversationSignalDetector`/`SkillExperienceOptimizer`/`OnlineEvolutionOrchestrator`/`EvolutionStore`/`ExperienceScorer`）+ `SkillEvolutionHook`（条件注册）+ 6 个 RPC + E/U/F 打分 + 反馈环 + 蒸馏。对应里程碑 M18 ✅。
- **Phase 18（Team 编排 MVP）已落地**：`team/` 包（`TeamManager` + `Team`）+ `delegate_to_member` 工具 + `TeamContextHook` + Leader/Member 双白名单 + 共享 workspace。对应里程碑 M22 ✅。
- **Phase 12–13、15–17、19 为后续规划**：Phase 12（中断恢复）→ Phase 13（文件快照与撤销）→ Phase 15（MCP 接入）→ Phase 16（DeepAgent 多轮外层循环）→ Phase 17（Deep Research）→ Phase 19（Team 协作核心）。参考 jiuwenswarm 对应能力设计。

---

## 阶段

### Phase 0 — 两进程骨架打通  `[已完成]`
**目标**：把 gateway↔agentserver 两进程 + 双向 ws + 单 web channel 跑通一条 echo。

内容：
- gateway 进程、agentserver 进程，各自启动。
- 双向 ws，定义消息信封（路由 / 会话 id / 流式分片）。
- web channel 接入 gateway（channel 适配层接口先埋，实现只 web 一种）。
- **关键命门**：先把 jiuwenswarm 的 gateway/server 接缝搞清——`gateway_push/`（server 侧主动推回 gateway）与 `agent_client.py`（gateway 侧调 server）的职责边界。Phase 0 必须定义清楚哪边负责什么，否则两进程拆分没有意义。

**验收**：浏览器发消息 → gateway ws → server ws → server 流式回 → 前端逐字显示。这条贯穿通是 Phase 0 唯一验收标准，没打通不进下一阶段。

---

### Phase 1 — agent loop 最小闭环 + 短期对话记忆  `[已完成]`
**目标**：接真模型，跑通 think→选工具→执行→结果回灌→再决策；同时落短期记忆（多轮对话记录）。

内容：
- **直接接真模型，不做 mock 阶段**。
- agent loop 最小闭环；1~2 个只读工具（如读本地文件、查天气）。
- **短期记忆落地**：session 对话记录、多轮上下文。这是 agent loop 多轮的必需件，不后置。
- **长期记忆用 stub**：`agent_manager` 调用长期 memory 的接口先空实现，埋好接口形状，后续不回炉。

**验收**：用户多轮提问 → 模型在跨轮上下文中正确判断是否调只读工具 → 调用 → 结果整合进回答 → 跨轮记住上文。

---

### Phase 2 — 工具系统成形  `[已完成]`
**目标**：从"能调一个工具"升级到"能管理一批工具并规划任务"。

内容：
- tool_manager：动态工具注册、四层框架（base/local_function/decorator/schema_extractor/manager）。
- 任务规划：`plan_todo_context`，把多步任务拆解、跟踪。
- tool 并发控制（`tool_concurrency`）——倾向先砍，单用户串行够用；如 Phase 2 出现并发需求再补。

**验收**：多工具正确选择 + 多步任务规划链路可用。

---

### Phase 3 — 长会话上下文压缩  `[已完成]`
**目标**：长对话不爆 token、不丢关键上下文。

**已落地**：`twinkle/agentserver/compression/` 算法包（滑窗 + LLM 摘要，**不写回 SessionStore**——history 无损，只塑形 LLM 输入）+ `hooks/builtin/context_compression_hook.py`（`ContextCompressionHook` priority 95，`before_model_call` 自动触发：token 估算超阈值时压缩 middle，保 head + 最近 tail，tool 配对不破；summary 失败降级为 head+tail）。`create_agent` 自动织入（与 SubagentContextHook 同款，无需 caller 传参）。spec `docs/superpowers/specs/2026-08-01-context-compression-hook-design.md`。

**验收**：单会话 100 轮对话不爆 token、关键事实不丢。 ✅

**后续优化方向（对齐 jiuwenswarm，部分已在 Phase 9 落地）**——参考 `docs/en/ContextCompression.md` + `jiuwenclaw/agentserver/deep_agent/rails/context_overflow_recovery_rail.py`（`enterprise_dev` 分支，`git show enterprise_dev:<path>` 读取）：
- **413 反应救火重试**（✅ 已在 Phase 9 落地 `ContextOverflowRecoveryHook`）：413 / `context_length_exceeded` 解析 token 数（Anthropic / OpenAI 格式），激进压缩（`keep_recent_pairs` 减半 + `threshold_override = limit × 0.85`）+ `request_retry()` 重试 + 连续 3 次失败熔断注入 `[CONTEXT_OVERFLOW]`。
- **触发条件多维度**（仍 deferred）：现仅 `estimate_tokens` 单阈值；加 message 计数维度、`large_message_threshold`（优先压大消息）、`offload_message_type`（可只压 tool 输出保对话）等旋钮。
- **窗口预算**（仍 deferred）：现固定阈值 60000；改为按模型窗口动态算（jiuwenswarm `threshold_override = 窗口 × 0.85`，预留 15% 给输出），随模型切换自适应。
- **异常降级增强**（仍 deferred）：现 summary 失败→丢 middle 保 head+tail；可加 offload 归档 + `[[OFFLOAD:...]]` 索引可检索召回（非丢弃），长会话早期被压事实能拉回。

---

### Phase 4 — 工具权限 / 审批 + 命令安全  `[已完成]`
**目标**：从"裸跑工具"升级到"工具可被策略管控，危险操作需审批"。

**已落地**：`twinkle/agentserver/permissions/` 包（models / builtin_rules / policy / audit / approval_registry / engine）+ `permission_context.py` ContextVar + `PermissionHook`（before_tool_call：ALLOW no-op / DENY force_finish / ASK raise HookInterrupt）。ASK 挂起/恢复用进程内 `ApprovalRegistry`（approval_id → asyncio.Future 单例）：`ReActAgent` 的 `except HookInterrupt` 注册 Future + yield `e2a.ask`（is_final=false）后 `await future` 挂起；`ws_handler` 内联路由 `approval.respond`（在 active-run guard 之前）→ resolve Future + 回 e2a.result ack（R2）；挂起的 `run()` 在**原 request_id**（R）上恢复 → 执行工具（allow）或注入 deny 消息回灌 → 再查模型。command_exec 的 blocklist 上提为 `builtin_rules.py` 单一真源（8 + 9 = 17 条），command_exec 与 PermissionPolicy 共读，disabled 模式下 command_exec 仍走它做 defense-in-depth。`TWINKLE_PERMISSIONS` 单 JSON env（对齐 OTEL opt-in），`enabled=false` 默认 = 系统关（全 ALLOW、无审计、无 ASK）。**精简范围仍 deferred**：shell AST 解析、三轴文件路径判定。spec `docs/superpowers/specs/2026-07-24-phase4-permissions-design.md`。

**验收**：危险工具调用前必须过策略；`require-approval` 工具触发用户审批卡；拒绝带 `[PERMISSION_DENIED]` 消息回灌；审计日志可查。 ✅

---

### Phase 5 — 长期记忆  `[已完成]`
**目标**：换掉 `memory.py` stub，agent 具备跨会话事实召回能力（RAG）。

**已落地**：`twinkle/agentserver/memory/` 包（`store.MemoryManager` 6 表 SQLite 混合检索：`chunks`/`chunks_fts`/`chunks_vec`/`embedding_cache`/`files`/`meta`；`sqlite-vec` 余弦向量 + FTS5 BM25 加权融合，无 provider / 无 sqlite-vec 自动降级 FTS-only；mtime 增量索引 + embedding cache + 模型变更重建 + 单文件 chunk FIFO 上限 + CJK 逐字分词救 FTS 召回）+ `embeddings.py`（`OpenAICompatibleEmbeddingProvider` 复用 `llm.api_key`/`llm.base_url`；`MockEmbeddingProvider` 仅测试）+ `get_memory_manager` 单例 + `tools/builtin/memory_tools.py`（`memory_search`/`write_memory`/`read_memory`/`edit_memory` @tool，**模型驱动，不自动注入**——MemoryHook 注入的是使用策略 prompt，不是召回结果）+ `hooks/builtin/memory_hook.py`（`MemoryHook` priority 80，`before_model_call` 注入策略 prompt，空 store no-op）+ `config.yaml` `memory:` 块 + `permissions.tools` 默认 4 个 memory tool=allow + `pyproject` `[memory]` extra（sqlite-vec）。DB 落 `<WORKSPACE>/.twinkle_data/memory/memory.db`。设计文档 `docs/design/memory-system-design.md`。

**验收**：跨会话记住事实（如"用户偏好/项目约定"），新会话里 `recall` 注入相关记忆进上下文；无 embedding 配置时降级到 FTS 仍可用。 ✅

**仍 deferred**：5b 自动抽取（对话自动写记忆）。~~5c Dreaming~~ **5c Dreaming 已落地**（B 方案 Phase 2，2026-08-17 重做：旧 N² pairwise `_dedupe_and_resolve` + 每段落 LLM 抽取判定为「真的很烂」已废，改 openclaw daily→MEMORY.md promotion 模型，见 `docs/design/dreaming-redesign.md`）：`memory/dreaming.py` 后台 asyncio task + busy-backoff，`dream()` = 扫 daily 非空行 → `claimHash` 跨文件去重 → 确定性门槛（≥`min_distinct_files` 个不同 daily）晋升 append 进 `MEMORY.md` + sidecar 记已晋升（只增不减，幂等）→ 单次 LLM 删行整合（`{"delete":[行号]}`，删冗余/矛盾行，≤25% 比例验证，fail-soft）→ 容量 compact 丢最老提升行。晋升零 LLM，整合 LLM 在后台不进写入路径。daily append-only 不动，opt-in 默认关。

---

### Phase 6 — 定时任务（cron）  `[已完成]`
**目标**：agent 能被定时唤醒执行任务，结果推送到通道。

**已落地**：`twinkle/gateway/cron/` 包（`CronSchedulerService` min-heap + croniter 算下一次执行时间，轮询 `cron_jobs.json` mtime 热加载；两阶段 wake→push：`wake_offset` 在 push 前先唤醒 agent，结果存 `CronRunState`，到 push 时间推到 targets 通道）+ `CronJobStore`（`<workspace>/cron_jobs.json` CRUD）+ `cron_models.py`（`CronJob`/`CronRunState`/`_Event`）+ `cron_expr.py`（croniter 解析）+ `twinkle/agentserver/tools/builtin/cron_tools.py`（5 个 @tool：`cron_list_jobs`/`cron_create_job`/`cron_update_job`/`cron_delete_job`/`cron_run_now`；`run_now` 写 sidecar 文件，gateway 检测后触发）+ `gateway/__main__.py` 启动时集成 `CronSchedulerService`。spec `docs/superpowers/specs/2026-07-29-cron-design.md`。

**验收**：注册一个 cron 任务，到点唤醒 agent 执行，结果推送到指定通道；支持单次任务与立即触发。 ✅

---

### Phase 7 — Skill 系统  `[已完成]`
**目标**：从"调原子工具"升级到"调用打包的知识+指令束（skill）"，支撑一类多步任务。

**已落地**：`twinkle/agentserver/skills/` 包（`Skill` + `SkillManager` 扫描/mtime 热重载/白名单 + `get_skill_manager` 单例）+ `tools/builtin/skill_tools.py`（`list_skill`/`read_skill` @tool）+ `hooks/builtin/skill_hook.py`（`SkillHook` priority 90，`before_model_call` 按 `TWINKLE_SKILL_MODE` 注入：`all`=每步注入清单 / `auto_list`=注入一句提示，默认 all）+ `<WORKSPACE>/skills/<name>/SKILL.md` 目录约定 + 示例 skill `doc-audit`（首次启动 seed）。`trigger` frontmatter 解析后丢弃（模型靠 description 自选，不做关键词自动匹配）。**SkillHub / SkillNet 双源下载安装已落地**：前端「🧩 技能」页支持两个公开 skill 源——SkillNet（`api-skillnet.openkg.cn`，搜索走公开 API、下载走 GitHub Contents/raw API）与 SkillHub（`api.skillhub.cn`，列表/搜索/zip 下载，SKILL.md 在 zip 根目录，与安装流程直接兼容），可切换源；安装到 `<WORKSPACE>/skills`，并支持 `skills.uninstall` 卸载。自实现不依赖 skillnet-ai。spec `docs/superpowers/specs/2026-07-27-skill-design.md` + `docs/superpowers/specs/2026-07-30-skillnet-download-design.md`。

**验收**：一个打包 skill 能被 agent 选中并读入上下文指导多步任务执行；skill 与 builtin tool 协同。 ✅

**仍 deferred**：`skill_turbo` planner/executor、marketplace/symphony（企业级）。skill 自进化见 Phase 14（已落地 v1）。

---

### Phase 8 — 子 Agent（subagent）  `[已完成]`
**目标**：从"单 agent 串行 ReAct"升级到"主 agent 可委派子 agent 并行/隔离执行子任务"，结果回灌由主 agent 整合。

**已落地**：`twinkle/agentserver/tools/builtin/subagent/` 包（`tools.py` 的 `spawn_subagent` @tool + `executor.py` 的 `SubagentExecutor` + `models.py` 的 `SubagentTaskSpec`/`SubagentResult`/`EXCLUDED_TOOLS` + `context.py` 的 ContextVar 隔离）+ `hooks/builtin/subagent_context_hook.py`（`SubagentContextHook` priority 50，`before_invoke` 设 executor + parent session/request id 到 ContextVar）。子 agent 即另开一个 `ReActAgent` 实例（复用 `LLMClient`/`SessionStore`，子 `ToolManager` 裁剪掉 `spawn_subagent`/`write_memory`/`edit_memory`），跑同一 `run()` 闭环，收敛后取 `e2a.complete` body 作 tool result。硬超时（300s）+ 软超时（120s 无流式响应）兜底；结果包 `[SYSTEM]` 停止提示防主 agent 重复委派；子 agent `max_steps=50`（更紧上限）。`create_agent` 自动构建 `SubagentExecutor` + 织入 `SubagentContextHook`。spec `docs/superpowers/specs/2026-07-28-subagent-design.md`。

**验收**：主 agent 调 `spawn_subagent` 委派子任务 → 子 agent 独立跑完 ReAct 收敛 → 结果回灌 → 主 agent 总结给用户；超时/异常有兜底不挂死主循环。 ✅

**仍 deferred**：`fork_agent`（消息前缀继承）、流式转发（事件转发到父流）、skill 声明角色（`SubagentConfig` frontmatter）、多级嵌套。team 编排见 Phase 18（已落地 MVP）。

---

### Phase 8a — Todo 增强  `[已完成]`
**目标**：将现有 Todo 系统从"扁平清单"增强为"结构化任务追踪"，对齐 jiuwenswarm 的 TodoItem + Claude Code 的 TaskCreate。

**已落地**：`twinkle/agentserver/todo/store.py`（`TodoTask` 数据模型增强：`id`(UUID)/`subject`/`description`/`blocked_by`/`owner`/`metadata`/`created_at`/`updated_at`；4 态：pending/in_progress/completed/cancelled；`TodoStore` API 重写：`create(subjects, sequential)`/`update(task_id, ...)`/`list(status?)`/`get(task_id)`）+ `twinkle/agentserver/tools/builtin/todo_tools.py`（4 个 @tool：`todo_create`/`todo_update`/`todo_list`/`todo_get`；`sequential=True` 一步创建线性依赖；轻量守卫：跳步检测 + 提醒，不拒绝）+ 前端 `TodoPanel.vue`（按状态分组 + 依赖/归属展示 + 脉冲动画）+ `webClient.ts`/`useSessions.ts` 类型更新。spec `docs/superpowers/specs/2026-08-01-todo-enhancement-design.md`。

**验收**：TodoTask 有唯一 ID、可设依赖、可追踪归属；`sequential=True` 一步创建线性依赖；前端按状态分组展示。 ✅

---

### Phase 9 — 上下文溢出恢复 + 重复调用检测  `[已完成]`
**目标**：agent 遇到上下文溢出时自动恢复，不再直接挂掉；agent 陷入重复工具调用循环时自动纠偏。

**已落地**：`twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`（`ContextOverflowRecoveryHook` priority 60，`on_model_exception` 检测 413/`context_length_exceeded`，3 层判定 + Anthropic/OpenAI 格式 token 解析，强制激进压缩（`keep_recent_pairs` 减半 + `threshold_override = limit_tokens × 0.85`）+ `request_retry()`，连续 3 次失败熔断注入 `[CONTEXT_OVERFLOW]` 消息）+ `twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py`（`RepeatToolCallDetectorHook` priority 88，`before_tool_call`/`after_tool_call`/`on_tool_exception` 记录 `(call_key, outcome_key)` 到 `deque(maxlen=30)` 滑动窗口，stable hash（SHA-256）+ 4 级分类（LOW: 同 call_key ≥ 10 → MEDIUM: A-B-A-B ≥ 10 → HIGH: 尾部连续相同 ≥ 20 → CRITICAL: ≥ 30），edge-triggered 只升不降，MEDIUM+ 自动注入 `[DETECTION]` 纠偏 system 消息，限频 5 次/分钟）+ `decorator.py` 改动（`ctx.extra["_tool_result"] = result` 传递 tool result 给 after-event hook）+ `config/schema.py` 新增 `OverflowRecoveryConfig` + `RepeatToolDetectionConfig`。spec `docs/superpowers/specs/2026-08-02-phase9-overflow-recovery-repeat-detection-design.md`。

**验收**：LLM 抛 413 时自动压缩重试成功；agent 重复调用相同工具触发循环检测（LOW ≥10 / HIGH ≥20）时自动注入纠偏消息跳出循环。 ✅

---

### Phase 10 — HITL 中断/恢复（跨请求断点续跑）  `[已完成]`
**目标**：用户审批中断后，关闭浏览器或服务重启，回来能从断点继续。

内容：
- **Approval 状态持久化**：当前 `ApprovalRegistry` 是纯内存 asyncio.Future，进程重启后丢失。改为持久化到 session 状态文件（`<session>/.approval_pending.json`），重启后恢复。
- **断点上下文保存**：`save_resume_ctx()` / `clear_resume_ctx()`，中断时保存 pending tool_call_id + 审批状态到 session，resume 时从 session state 取回。
- **前端重连恢复**：WebSocket 重连后，服务端检查 session 是否有 pending approval，推送 `approval.ask` 事件让前端显示审批卡片。
- **对齐 jiuwenswarm**：`permission_bridge.py` 的 `save_resume_ctx()`/`clear_resume_ctx()`（`jiuwenclaw/agentserver/replan_agent/permission_bridge.py`）。

**验收**：用户审批中断 → 关闭浏览器 → 重新打开 → 看到 pending 审批卡片 → 点击允许 → agent 从断点继续执行。

---

### Phase 11 — Workflow 引擎（PlanNode 递归执行树 + Fallback）  `[已完成]`
**目标**：从"LLM 自驱的灵活规划"升级到"引擎驱动的可靠编排"，支撑固定流程的复杂任务（如 PPT 生成）。

**已落地**：`twinkle/agentserver/workflow/` 包——
- **`PlanNode` 基类**（`node.py`）：ABC，子类实现 `async _execute(inputs) -> Any`，`run()` 模板方法不可覆盖，自带 fallback。节点 `plan_name`/`instruction`/`sub_plans`（递归树）；能力经回调注入 `call_tool`/`call_llm`/`extract_json`（`set_runtime_callbacks` 递归传播），不 import 系统模块；`HookInterrupt` 透传不走 fallback（= roadmap 原 AbortError 语义）。
- **`WorkflowExecutor`**（`executor.py`）：`execute_workflow(plan_code, inputs)` → validate → 沙箱 exec plan_code 提取 `root` PlanNode → 绑回调 → `root.run(inputs)` 带 `asyncio.wait_for` 超时。
- **安全沙箱**（`sandbox.py`）：`_SAFE_BUILTINS` 白名单（无 `open/exec/eval/getattr/type`）+ `_FORBIDDEN_MODULES`（禁 os/sys/subprocess/socket/urllib）+ `_ALLOWED_IMPORT_PREFIXES=("twinkle.agentserver.workflow",)` + `_SafeAsyncio`（禁 create_subprocess）。
- **`PlanCodeValidator`**（`validator.py`）：AST 级校验——禁裸 import/相对 import、import 前缀限白名单、禁 exec/eval/compile/open/getattr/type/__import__、禁 dunder 逃逸属性（`__globals__/__code__/__subclasses__` 等）。
- **Fallback**（`_fallback_wrapper`）：节点失败 → 计数 → 超 `max_fallback_count` 抛 `FallbackLimitExceededError`；基础设施错误（Connection/Auth/RateLimit/Timeout 类）短路重抛（subagent 调同 LLM 同样会失败、免烧 token）；否则委派 `SubagentExecutor` 兜底执行 `node.instruction`。
- **工具**：`execute_workflow(workflow_name, inputs)` @tool（`workflow/tools.py`），从 `<WORKSPACE>/workflows/<name>/root.py` 读 plan_code + 路径遍历防护；**动态工具描述**扫描 workflows 目录拼可用清单，LLM 据此自选。
- **Hook**：`WorkflowContextHook`（priority 50，`before_invoke` 设 `workflow_executor_ctx` ContextVar）；`WorkflowExecutor` 始终在 `create_agent` 织入。
- **内置 workflow**：`pptx-craft`（`workflow/ppt/root.py`，7 节点流水线：IntentClassify→…→PPTExport→Delivery，python-pptx 导出 .pptx，支持 spec-mode），`workspace.py` `_seed_bundled_workflows` 启动 seed 到 `<WORKSPACE>/workflows/`。

spec `docs/superpowers/specs/2026-08-03-phase11a-workflow-engine.md` + `2026-08-03-phase11b-ppt-generation.md`。

**验收**：3 层 PlanNode 树中间节点失败自动 fallback；节点间 inputs 显式传递；沙箱拒绝 import os/subprocess；fallback 超限抛错；HookInterrupt 透传。 ✅（测试 `tests/test_workflow_e2e.py`/`test_plan_sandbox.py`/`test_plan_node.py`/`test_workflow_pptx.py`）

**仍 deferred**：RePlan 第二步——LLM 自动生成 plan_code（当前 root.py 全手写）。

---

### Phase 12 — 中断恢复（对话历史驱动）
**目标**：agent 在任何中断（模型报错、用户停止、进程崩溃、审批中断）后，用户说「继续」或发新消息，LLM 能从对话历史自然恢复，不丢失任务上下文。

**设计理念**：对齐 Claude Code 的中断恢复模式——**对话历史就是状态**，不需要额外的 `DeepAgentState` 或 `Checkpointer`。LLM 读到中断标记就能理解发生了什么、从哪里继续。与 jiuwenswarm 的 `DeepAgentState` + `save_state/load_state`（为外层循环的循环变量设计）不同，Twinkle 当前是单轮 ReAct 架构，所有「状态」都在对话历史 + TodoStore + 审批文件中，不需要额外的状态持久化机制。

内容：
- **中断标记写入**：`run()` 的 `finally` 块中，如果请求不是正常完成（模型报错、异常中断、用户停止等），往 session 写一条 assistant 标记消息，包含中断原因和当前上下文。这样不管用户下次说什么，LLM 都能看到中断信息。
- **`_sanitize_orphan_tool_calls` 升级**：从 session 历史推导更丰富的中断上下文——不仅注入 `[interrupted]`，还包含：被中断的工具名和参数、当前 Todo 进度（从 TodoStore 读）、审批中断的 reason（从 `.approval_pending.json` 读）。
- **审批中断恢复**：进程崩溃后 `.approval_pending.json` 中的审批记录无法恢复 `asyncio.Future`。改为在 `_sanitize_orphan_tool_calls` 中读取审批记录，注入 `[interrupted: approval was pending (reason: ...)]`，让 LLM 重新决策（重新请求审批或换方案），而非尝试恢复 Future。
- **模型失败标记**：模型 API 报错/超时后，session 里只有 user 消息没有 assistant 回复。在 `except Exception` 的 `raise` 前往 session append 一条 assistant 标记 `[SYSTEM] 模型调用失败（...）`，避免 LLM 看到连续两条 user 消息而困惑。

**两条恢复路径**：
- **路径 A（正常中断）**：`run()` 的 `finally` 块能执行 → 实时写入中断标记。覆盖：模型报错、用户停止、Gateway 断连、审批中断。
- **路径 B（进程崩溃）**：`finally` 块来不及执行 → 下次请求时 `_sanitize_orphan_tool_calls` 从历史 + TodoStore + 审批文件推导上下文补写。覆盖：进程崩溃、kill -9、断电。

**用户行为无关性**：中断标记是 assistant 消息，不强制用户继续。用户说「继续」→ LLM 恢复任务；用户说别的 → LLM 回答新问题，不主动提旧任务。与 Claude Code 的 Ctrl+C → 换话题行为一致。

**验收**：
1. agent 执行到第 3 步时模型报错 → session 里有中断标记 → 用户说「继续」→ LLM 从中断点恢复执行。
2. agent 执行到第 5 步时进程崩溃 → 重启 → 用户发新消息 → `_sanitize_orphan_tool_calls` 补写上下文 → LLM 知道上次在做什么、Todo 进度到哪了。
3. agent 审批中断后进程崩溃 → 重启 → 审批记录转化为 `[interrupted]` 消息 → LLM 重新决策。
4. 用户中断后不说继续，而是问新问题 → LLM 正常回答新问题，不主动提旧任务。

---

### Phase 13 — 文件快照与撤销（Claude Code 风格 Checkpoint）
**目标**：agent 修改文件后，用户可以撤销（undo）改动，回退到之前的版本。对齐 Claude Code 的 `/rewind` + Checkpoint 机制。

**设计理念**：Claude Code 的 Checkpoint 是**文件状态快照**——每次用户发消息时自动保存被修改的文件内容，支持选择性回退（恢复代码、恢复对话、压缩上下文等）。Twinkle 采用更轻量的方案：只存写操作前的文件内容，支持 LLM 驱动或用户主动的撤销。

内容：
- **FileSnapshotStore**：在 `write_file` / `edit_file` 执行前，保存旧文件内容到 per-session 快照文件（`<session_dir>/.file_snapshots/<timestamp>_<hash>.json`，内容：`{path, content, timestamp, tool_call_id}`）。新文件创建不存快照（无旧内容可回退）。
- **`undo_file` 工具**：`@tool`，读取最近的快照，恢复旧内容。LLM 可在用户说「撤销」时调用。参数：`file_path`（可选，不传则恢复最近一次修改）、`steps`（回退几步，默认 1）。
- **`file_history` 工具**：`@tool`，列出某个文件的修改历史（时间戳 + 工具调用摘要），供 LLM 或用户选择回退到哪个版本。
- **快照清理**：session 结束时清理快照文件；快照数量上限（如 100），超出时 FIFO 清理。
- **局限性**：`command_exec` 的副作用（如 `rm`、`mv`）不在快照范围内——与 Claude Code 一致，Bash 命令的副作用不可自动撤销。用户应依赖 Git 做最终版本管理。

**对齐 Claude Code**：Claude Code 的 Checkpoint 在每次用户发消息时存全量快照，支持 5 种精细化回退模式（恢复代码+对话 / 仅恢复对话 / 仅恢复代码 / 从此处总结 / 到此处总结）。Twinkle 的轻量版只做文件内容回退，不做对话回退（对话历史是 append-only，不支持删除中间消息）。

**验收**：
1. agent 调 `write_file` 修改了 `report.md` → 用户说「撤销」→ LLM 调 `undo_file` → 文件恢复到修改前的内容。
2. agent 连续修改了 3 个文件 → 用户说「撤销刚才的修改」→ LLM 调 `undo_file` 恢复最近一次修改。
3. agent 修改了 `report.md` 两次 → 用户说「回到第一个版本」→ LLM 调 `file_history` 查看 → 调 `undo_file(steps=2)` 回退到最初版本。

---

### Phase 14 — Skill 自进化 v1  `[已完成]`
**目标**：skill 定义能根据运行反馈自动改进。

**已落地**：`twinkle/agentserver/evolution/` 包——命名自定，不用 jiuwenswarm 的 EvolutionRail/SkillEvolver/SignalDetector。
- **信号检测**（`signal_detector.py` `ConversationSignalDetector`）：纯正则+路径匹配，不调 LLM。`execution_failure`（扫 tool result 命中 `error/exception/failed/timeout/traceback/...`，默认开）、`script_artifact`（command_exec 等成功且内容>20 字，默认开）、`user_intent`（纠正词 `不对/应该是/...`，默认关）。从 tool_call 参数反推活跃 skill（SKILL.md 路径正则 + skill_name + 内容 fallback）。
- **经验生成**（`optimizer.py` `SkillExperienceOptimizer`）：LLM 产 JSON draft，硬上限 text≤2/script≤1；去重+优先级筛选委托 prompt（priority「导致失败 > 低效但成功；高频 > 偶发」），`merge_target` 改写已有记录。
- **打分**（`scorer.py` `ExperienceScorer`）：E(贝叶斯效能)+U(利用率)+F(90 天半衰期新鲜度)+版本不匹配惩罚。
- **审批**（`orchestrator.py` `OnlineEvolutionOrchestrator`）：手动 pending（内存 dict，**v1 不持久化**）+ `evolve_pending`/`evolve_approve`/`evolve_reject` RPC；`auto_save=true` 走自动落盘（见 deferred——当前单例硬编码 false）。
- **持久化**（`store.py` `EvolutionStore`）：`<skills>/<name>/evolutions.json` 原子写（temp+fsync+replace）；`render_evolution_markdown` 往 `SKILL.md` 注 `<!-- evolution-index-start -->…end -->` 索引块 + 正文写 `evolution/<section>.md` sidecar + 脚本工件写 `evolution/scripts/`。经验**外挂**，不 merge 进 SKILL.md 正文（`read_pristine_skill_content` 可剥索引块供分享）。
- **反馈环**：`run_feedback_loop` 注入后对话片段送 LLM 判 used/positive/negative → 回写 UsageStats → 重算分。
- **蒸馏**：`orchestrator.simplify()` LLM 给 DELETE/MERGE/REFINE/KEEP，分<min_score 且零调用规则前置直接 DELETE。
- **Hook**：`SkillEvolutionHook`（priority 80，`before_model_call` 注入 top-3 高分经验 + `after_invoke` 遍历所有 skill 跑 evolve）；`server.py` `if EVOLUTION_ENABLED` 条件注册（`evolution.enabled` 默认 false = opt-in）。
- **暴露**：6 个 E2A RPC（`skills.evolve`/`evolve_list`/`evolve_simplify`/`evolve_pending`/`evolve_approve`/`evolve_reject`），**非 `@tool`**，LLM 不能自主触发。

落地 commit `f89fcb5`。测试 37 个（`tests/test_evolution_{types,signal,store,scorer}.py`，覆盖纯函数部分）。

**验收**：跑失败的任务产出 skill 演进经验，经审批持久化到 sidecar + 索引块，`before_model_call` 注入高分经验。 ✅

**仍 deferred**：① `config.evolution.trigger` 四档挂点切换（声明 but hook 只实现 after_invoke，**死配置**）；② `auto_save`/`max_*`/`scoring.*` 旋钮接通（单例不读 config，**死配置**）；③ pending 持久化跨进程恢复；④ `solidify` 经验回融 SKILL.md 本体（当前外挂）；⑤ `/evolve` 斜杠命令（当前是 RPC 形态）；⑥ optimizer/orchestrator/hook 集成测试；⑦ 成功率回归埋点。

---

### Phase 15 — MCP 工具接入
**目标**：让 twinkle 能挂载标准 MCP（Model Context Protocol）server 的工具，补足工具生态。

内容：
- 从 config 读 `mcp.servers`，转 `McpServerConfig`（stdio / sse transport）。
- 把 MCP server 暴露的工具注册进 `ToolManager`（复用现有 `schemas()` / `execute()` 面，`ReActAgent` 零改动）。
- MCP 工具受 Phase 4 权限策略统一管控。
- **为何后置**：MCP 是纯扩展性 nice-to-have（builtin 工具已覆盖读写/搜索/执行），优先级低于让 agent 自主跑起来的能力。

**验收**：在 config 配一个 MCP server，agent 能像调 builtin 工具一样调其工具；权限策略对 MCP 工具同样生效。

---

### Phase 16 — DeepAgent 多轮外层循环 + 停止条件
**目标**：从"单轮 ReAct"升级到"多轮迭代直到任务完成"，支持复杂多步任务的可靠执行。

内容：
- **DeepAgent 外层循环**：包装现有 `ReActAgent`，在单次 `run()` 收敛后判断是否需要继续迭代。`LoopCoordinator` 跟踪迭代计数、token 预算、wall-clock 时间。
- **TaskCompletionRail + StopConditionEvaluator**：可组合的停止条件链（OR 语义）：`MaxRoundsEvaluator`（最大轮数）、`TimeoutEvaluator`（超时）、`TokenBudgetEvaluator`（token 预算）、`CompletionPromiseEvaluator`（LLM 主动输出 `<promise>TASK_DONE</promise>` 信号完成）。
- **TaskPlan 集成**：与 Phase 8a 的 Todo 系统集成，每轮迭代后同步 todo 状态。
- **对齐 jiuwenswarm**：`DeepAgent`（`openjiuwen/harness/deep_agent.py`）+ `LoopCoordinator`（`openjiuwen/harness/task_loop/loop_coordinator.py`）+ `TaskCompletionRail`（`openjiuwen/harness/rails/task_completion_rail.py`）。

**范围控制**：先做基础外层循环 + 停止条件；follow-up 队列、事件驱动执行等列为后续。

**验收**：agent 执行多步任务时，完成一个子任务后自动进入下一轮迭代，直到 LLM 输出完成信号或触发停止条件；超时/最大轮数有兜底。

---

### Phase 17 — Deep Research（深度研究任务管理器）
**目标**：agent 能执行多步检索→分析→综合→报告的深度研究任务，结果异步推送。

内容：
- **DeepResearchTaskManager**：后台异步执行，支持多步检索+分析+综合+导出。
- **工具集**：`deepresearch_create_task`/`deepresearch_status`/`deepresearch_cancel`/`deepresearch_get_result`。
- **结果推送**：完成后通过 WebSocket 推送，cron 可定期触发。
- **对齐 jiuwenswarm**：`DeepResearchTaskManager`（`jiuwenclaw/agentserver/tools/deepresearch_task_manager.py`）+ 工具（`jiuwenclaw/agentserver/tools/deepresearch_tools.py`）。

**验收**：用户说"帮我分析 XX 行业趋势"→ agent 创建深度研究任务 → 多步检索+分析 → 生成结构化报告 → 推送结果。

---

### Phase 18 — Team 编排 MVP（多 Agent 协作）  `[已完成]`

**目标**：1 leader + 动态角色化 member 并发执行子任务，leader 整合结果输出最终答案。

**已落地**（commit `f7fcb6a`）：独立 Team 子系统，**不复用 SubagentExecutor**——`twinkle/agentserver/team/` 包：
- **`TeamManager`**（`manager.py`）：全局单例注册表 session_id→Team，`server.py` 启动 always wired，只在 `request.mode=="team"` 激活。
- **`Team`**（`manager.py`）：per-session，管理成员 `ReActAgent` + 委派；`_member_key` blake2b 哈希 persona 做跨进程确定性 key，member session id=`{sid}__team_{key}`。
- **`delegate_to_member(persona, objective, prompt)` @tool**（`team_tools.py`）：thin wrapper 从 `CURRENT_TEAM` ContextVar 读 Team → `Team.delegate()` → `_drive_member()`（复刻 SubagentExecutor 的 child-task ContextVar 隔离 + queue drain + soft/hard/abort timeout + 结果截断，**自己实现不调 SubagentExecutor**）。**persona 由 LLM 动态发明**（自由文本），非预定义 role 名。
- **ContextVar 桥**（`context.py`）：`CURRENT_TEAM`（`TeamContextHook` priority 45，`before_invoke` 按 mode set）+ `MEMBER_WORKSPACE`（`_drive_member` 在 child task set，file_tools 据此把成员写操作重定向到 team 目录）。
- **共享 workspace**（`workspace.py`）：`<WORKSPACE>/team/<session_id>/shared/`，`ensure_team_workspace` 幂等创建。
- **Leader = 纯协调者**（`agent.py`）：`build_leader_system_prompt()`（角色/职责/决策原则/工作流程）+ `_TEAM_LEADER_TOOL_WHITELIST`（仅 delegate_to_member + todo + 只读工具，**刻意排除 command_exec/write_file/edit_file** → leader 必须委派）。
- **Member**（`agent.py`）：`build_member_system_prompt(persona, workspace)`（团队角色+persona+共享工作区）+ `build_agent_runtime_prompt()`（lean 运行环境，不含用户面身份规则/全局工作区路径）+ `MEMBER_TOOL_WHITELIST`（共享同一套，含执行工具，排除 write_memory/edit_memory、spawn_subagent、delegate_to_member、execute_workflow 防递归）。`is_team_mode` 检测 → 切 leader prompt + 过滤工具白名单。
- **`_merge_system_messages`**：识别 `# 团队角色` 为身份前缀（与 `# 身份与行为原则` 同列）。
- **`TeamConfig`**（`config/schema.py`）：仅 `enabled: bool`（**无 MemberSpec YAML**，persona 动态、白名单硬编码 frozenset）。
- **前端**：`ChatPanel.vue` session header team mode 选择器。

spec `docs/superpowers/specs/2026-08-05-team-collaboration-analysis.md`。测试 28 个（`tests/test_team.py`：TeamManager 生命周期/成员构建/委派/leader-member prompt/双白名单/ContextVar）。

**与原规划偏差**：原计划复用 SubagentExecutor + `spawn_subagent(role)` + MemberSpec YAML（role 名→服务端查 config）；实际走独立 Team 子系统 + 动态 persona + 硬编码白名单。前置依赖也变：**不依赖 Phase 16**（Team 直接用 ReActAgent step 循环）。

**验收**：用户说"写一份 AI safety 报告" → leader 委派 researcher + writer（各为独立 member agent）→ 产出到 team/shared/ → leader 整合输出。member 调 `delegate_to_member`/`spawn_subagent` 被拒（白名单排除，防递归）。 ✅

**仍 deferred**：任务队列+认领+member 身份+leader→member steer（Phase 19 做，见下）、成员间直接通信、Monitor 事件流（14 种事件）、`e2a.team_event` 新帧类型+Gateway 映射、`TeamRecoveryManager` 成员崩溃恢复、前端 Team 面板（均更后 defer）。

---

### Phase 19 — Team 协作核心（多 Agent 协作）

**目标**：在 Phase 18 同步委派之上加任务队列编排 + leader→member steer 注入 + member 身份，跑通单进程内任务驱动型多 agent 协作核心机制（任务分解/认领/依赖/状态流转/运行时动态注入/寻址）。**显式 defer** 成员间 P2P/Broadcast、Monitor 事件流、崩溃恢复、前端面板——需 member 常驻/并发或属可观测/可靠性/前端范畴，另阶段补。

内容（spec `docs/superpowers/specs/2026-08-07-phase19-team-collaboration-core-design.md`）：
- **TeamTaskStore**（`team/task_store.py`）：复用 TodoStore 单例（按 team `session_id` 存），加编排层——claim 独占校验、依赖解除、依赖图环检测（DFS）。复用 4 态（pending/in_progress/completed/cancelled）+ blocked 派生态，不新增状态。
- **member 身份**：`member_name`（leader 显式命名，稳定可读）替代 persona hash 作 member_key/寻址；persona 降为 prompt 个性化。`_member_key`/`_member_session_id`/`delegate`/`delegate_to_member` 签名加 `member_name`。
- **member inbox + steer 注入**：每 member 一个 `asyncio.Queue`；`ReActAgent.__init__` 加可选 `inbox`，run 循环每步 drain，新消息作 user input 注入当前 round **不进 session store**（不污染历史/不膨胀）。leader `send_message(to=member_name, content)` 投递。
- **leader 不收消息通道**：复用 Phase 18 同步 delegate；member→leader 全走 task list——求助=标 blocked+原因+主动结束 run → delegate 返回 → leader `list_tasks` 处理。
- **team task 工具**（`team_tools.py`）：`create_task`/`claim_task`/`complete_task`/`cancel_task`/`list_tasks`/`get_task`/`send_message`，按 Leader/Member 双白名单配置（leader 只协调不 claim/complete，member 不能 create/cancel）。
- **member 退出释放认领**：member run 结束（正常/超时/错误）→ Team 自动释放其 claim 未 complete 的 task（owner 清空、回 pending）。

**仍 deferred**（spec §10）：member 间 P2P/Broadcast（需 member 常驻/并发）、plan mode、stale sweep、Monitor 事件流（14 种）+ `e2a.team_event` 新帧+Gateway 映射、TeamRecoveryManager 成员崩溃恢复、前端 Team 面板、team 记忆只读优化（给 Leader 加 `write_memory`）。

**演进方向**（spec §11）：Phase 19 是向 jiuwenswarm 收敛的第一步；后续补编排能力统一做成独立组件（SpawnManager/RecoveryManager/CoordinationKernel/StreamController/SessionManager/EventBus），Team 保持纯容器不养厚（不养成「没继承 BaseAgent 的伪 TeamAgent」）。当前 Team 不继承 agent 的「干净」部分是能力不足副产品（不 invoke Team 故无 LSP 张力），不是设计胜利。追上 jiuwenswarm 的标志：team 整体可 invoke/stream + member 自治 + 崩溃恢复 + 事件可观测。

**前置依赖**：Phase 18 跑通（复用 delegate 通路 + Team/TodoStore 基建；不依赖 Phase 12，崩溃恢复另阶段补）。

**验收**：用户要 team「调研 X 并写报告」→ leader `create_task`(T1 调研, T2 写报告 `blocked_by=[T1]`) + `create_member`(researcher/writer) → researcher claim T1→complete（触发 T2 依赖解除）→ 结束 run → leader `list_tasks` → delegate writer → writer claim T2→`get_task(T1)` 拿结果→写报告→complete → 全完成→leader 综合回答。测试覆盖状态机/claim 独占/环检测/依赖解除/退出释放/steer 不进 session/求助流转/超时（spec §8，8 类）。

---

## 跨阶段基础设施

以下能力不在单一 Phase 中，而是随各 Phase 逐步积累形成的基础设施层：

### Hook 系统  `[已落地]`
Phase 4 引入最小钩子点后，逐步发展为完整的 Hook 框架：
- **`twinkle/agentserver/hooks/`** 包（`base.py` 的 `AgentHook` 基类 + `manager.py` 的 `HookManager` 优先级排序 + `decorator.py` 的 `@hook` 装饰器）
- **11 个 builtin hook + `WorkflowContextHook`**（运行时共 12 个，按 priority 降序）：
  - `PermissionHook`（priority 100，before_tool_call 权限拦截）
  - `ContextCompressionHook`（priority 95，before_model_call 自动压缩）
  - `SkillHook`（priority 90，before_model_call skill 注入）
  - `RepeatToolCallDetectorHook`（priority 88，before/after_tool_call 循环检测 + before_model_call 纠偏注入）
  - `SkillEvolutionHook`（priority 80，before_model_call 注入高分经验 + after_invoke 跑进化；`if EVOLUTION_ENABLED` 条件注册）
  - `MemoryHook`（priority 80，before_model_call 记忆策略注入）
  - `ContextOverflowRecoveryHook`（priority 60，on_model_exception 溢出恢复 + after_model_call 计数重置）
  - `SubagentContextHook`（priority 50，before_invoke ContextVar 桥接）
  - `WorkflowContextHook`（priority 50，before_invoke 设 `workflow_executor_ctx`；在 `workflow/tools.py`，always wired）
  - `RetryHook`（priority 50，transient 异常自动重试）
  - `TeamContextHook`（priority 45，before_invoke 按 mode 设 `CURRENT_TEAM` ContextVar；always wired）
  - `LoggingHook`（priority 10，LLM/tool 调用日志）
- **事件**：`before_invoke`/`after_invoke`/`before_model_call`/`after_model_call`/`on_model_exception`/`before_tool_call`/`after_tool_call`/`on_tool_exception`
- **中断机制**：`HookInterrupt`（PermissionHook ASK 挂起/恢复）

### YAML 配置系统  `[已落地]`
- **`twinkle/config/`** 包（`schema.py` 的 pydantic 严格模型 + `loader.py` 的 YAML/env 加载）
- **优先级**：环境变量 > `.env` 文件 > `config.yaml` 默认值
- **配置块**：agentserver / gateway / workspace / logging / sessions / todos / llm / agent / context_compression / skills / memory / permissions / subagent / overflow_recovery / repeat_tool_detection / workflow / evolution / team
- spec `docs/superpowers/specs/2026-07-27-yaml-config-design.md`

### Web 工具  `[已落地]`
- **`web_fetch`**：httpx 异步抓取 + HTML→markdown + 长度截断 + Tavily extract fallback（anti-bot 403）
- **`web_search`**：Tavily 主力 + DDG fallback（无 key 时自动降级）+ max_results 控制

### 并行工具执行  `[已落地]`
- `agent.py` 的 `ReActAgent` 在同一 `tool_calls` 内多个工具调用时使用 `asyncio.gather` 并行执行，不串行等待

---

## 里程碑

| 里程碑 | 验收标准 | 状态 |
|---|---|---|
| M1 两进程通 | ws echo 贯穿 web↔gateway↔agentserver | ✅ |
| M2 能调工具 | 真模型 + 只读工具闭环 + 多轮上下文 | ✅ |
| M3 能管工具 | 多工具选择 + 任务规划 | ✅ |
| M4 能扛长会话 | 100 轮不爆 token、不丢关键事实 | ✅ |
| M5 工具可管控 | 危险工具审批 + 命令安全 + 审计日志 | ✅ |
| M6 有长期记忆 | 跨会话事实召回 + RAG 注入 | ✅ |
| M7 会定时跑 | cron 唤醒 agent + 结果推送通道 | ✅ |
| M8 能用 skill | skill 加载 / 选择 / 注入指导多步任务 | ✅ |
| M9 能委派子 agent | spawn 委派 + 结果回灌 + 超时隔离 | ✅ |
| M9a Todo 增强 | 结构化任务追踪 + 依赖 + 归属 | ✅ |
| M13 溢出恢复 + 循环检测 | 413 自动重试 + 重复调用纠偏 | ✅ |
| M14 中断可恢复 | 审批中断后关闭浏览器回来能继续 | ✅ |
| M15 引擎驱动编排 | PlanNode 树 + fallback + 沙箱 | ✅ |
| M16 中断可恢复 | 中断标记 + `_sanitize_orphan_tool_calls` 升级 + 审批中断恢复 | |
| M17 文件可撤销 | 文件快照 + undo_file + file_history | |
| M18 skill 会进化 | 信号检测→经验生成→审批→sidecar 持久化（v1） | ✅ |
| M19 能挂外部工具 | MCP server 工具接入并受策略管控 | |
| M20 多轮迭代 | DeepAgent 外层循环 + 停止条件链 | |
| M21 深度研究 | 多步检索→分析→综合→报告 | |
| M22 多 Agent 协作 MVP | leader + 动态 persona member 并发委派 | ✅ |
| M23 多 Agent 协作核心 | Team 任务队列 + 成员通信 + 认领/依赖 | |
| M12 可观测 | OTel span 链 + 关键指标 | ✅ |

---

## 与 jiuwenswarm 参考实现的关系

- **学思想、借模式，不照搬依赖 openjiuwen 生态的实现**（manifest catalog / 分布式 swarm / symphony）。
- 每个 Phase 的"参考实现"锚点见对应小节；主分支源码仅 `.pyc`，`.py` 源码在 `enterprise_dev` 分支用 `git show enterprise_dev:<path>` 读取。
- 各模块对照见 `docs/architecture.md` §11；模块行为不清时查参考实现对应文件。
