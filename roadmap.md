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
- 工具系统（四层 ToolManager + `@tool` + 24 个 builtin 工具：web_fetch/web_search/command_exec/file_ops(5)/todo(4)/skill(2)/memory(4)/cron(5)/subagent(1)）
- Hook 系统（7 个 builtin hook：Permission/Skill/Memory/Compression/Retry/Logging/SubagentContext）
- 短期记忆（SessionStore 多轮对话记录）
- 长期记忆（MemoryManager SQLite 混合检索 + 4 个 memory 工具）
- 长会话上下文压缩（ContextCompressionHook 自动压缩）
- 单 web channel（+ channel 扩展点）
- **OTel 遥测切面**（`observability/` 包：`setup()` 在 `agentserver/__main__` 启动时接入，monkey-patch `AgentLoop.run_stream` / `LLMClient.stream` / `ToolManager.execute` 三个 choke point 造 span，OTLP gRPC / console / none 导出 + 指标；`OTEL_ENABLED` 默认 false 为零成本 no-op）

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
- **Phase 11–18 为后续规划**：Phase 11（PlanNode 递归执行树）→ Phase 12（中断恢复）→ Phase 13（文件快照与撤销）→ Phase 14（Skill 自进化）→ Phase 15（MCP 接入）→ Phase 16（DeepAgent 多轮外层循环）→ Phase 17（Deep Research）→ Phase 18a（Team 编排 MVP）→ Phase 18b（Team 编排完整版）。参考 jiuwenswarm 对应能力设计。

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

**已落地**：`twinkle/agentserver/compression/` 算法包（滑窗 + LLM 摘要，**不写回 SessionStore**——history 无损，只塑形 LLM 输入）+ `hooks/builtin/context_compression_hook.py`（`ContextCompressionHook` priority 95，`before_model_call` 自动触发：token 估算超阈值时压缩 middle，保 head + 最近 tail，tool 配对不破；summary 失败降级为 head+tail）。`build_agent_loop` 自动织入（与 SubagentContextHook 同款，无需 caller 传参）。spec `docs/superpowers/specs/2026-08-01-context-compression-hook-design.md`。

**验收**：单会话 100 轮对话不爆 token、关键事实不丢。 ✅

**后续优化方向（对齐 jiuwenswarm）**——参考 `docs/en/ContextCompression.md` + `jiuwenclaw/agentserver/deep_agent/rails/context_overflow_recovery_rail.py`（`enterprise_dev` 分支，`git show enterprise_dev:<path>` 读取）：
- **413 反应救火重试**：LLM 抛 413 / `context_length_exceeded` 时解析 token 数（Anthropic / OpenAI / 华为三种格式），强压 + `request_retry()` 重试 + 连续失败熔断。初版只赌主动压缩能防住，赌错则请求直接挂。
- **触发条件多维度**：现仅 `estimate_tokens` 单阈值；加 message 计数维度、`large_message_threshold`（优先压大消息）、`offload_message_type`（可只压 tool 输出保对话）等旋钮。
- **窗口预算**：现固定阈值 60000；改为按模型窗口动态算（jiuwenswarm `threshold_override = 窗口 × 0.85`，预留 15% 给输出），随模型切换自适应。
- **异常降级增强**：现 summary 失败→丢 middle 保 head+tail；可加 offload 归档 + `[[OFFLOAD:...]]` 索引可检索召回（非丢弃），长会话早期被压事实能拉回。

---

### Phase 4 — 工具权限 / 审批 + 命令安全  `[已完成]`
**目标**：从"裸跑工具"升级到"工具可被策略管控，危险操作需审批"。

**已落地**：`twinkle/agentserver/permissions/` 包（models / builtin_rules / policy / audit / approval_registry / engine）+ `permission_context.py` ContextVar + `PermissionHook`（before_tool_call：ALLOW no-op / DENY force_finish / ASK raise HookInterrupt）。ASK 挂起/恢复用进程内 `ApprovalRegistry`（approval_id → asyncio.Future 单例）：`agent_loop` 的 `except HookInterrupt` 注册 Future + yield `e2a.ask`（is_final=false）后 `await future` 挂起；`ws_handler` 内联路由 `approval.respond`（在 active-run guard 之前）→ resolve Future + 回 e2a.result ack（R2）；挂起的 run_stream 在**原 request_id**（R）上恢复 → 执行工具（allow）或注入 deny 消息回灌 → 再查模型。command_exec 的 blocklist 上提为 `builtin_rules.py` 单一真源（8 + 9 = 17 条），command_exec 与 PermissionPolicy 共读，disabled 模式下 command_exec 仍走它做 defense-in-depth。`TWINKLE_PERMISSIONS` 单 JSON env（对齐 OTEL opt-in），`enabled=false` 默认 = 系统关（全 ALLOW、无审计、无 ASK）。**精简范围仍 deferred**：shell AST 解析、三轴文件路径判定。spec `docs/superpowers/specs/2026-07-24-phase4-permissions-design.md`。

**验收**：危险工具调用前必须过策略；`require-approval` 工具触发用户审批卡；拒绝带 `[PERMISSION_DENIED]` 消息回灌；审计日志可查。 ✅

---

### Phase 5 — 长期记忆  `[已完成]`
**目标**：换掉 `memory.py` stub，agent 具备跨会话事实召回能力（RAG）。

**已落地**：`twinkle/agentserver/memory/` 包（`store.MemoryManager` 6 表 SQLite 混合检索：`chunks`/`chunks_fts`/`chunks_vec`/`embedding_cache`/`files`/`meta`；`sqlite-vec` 余弦向量 + FTS5 BM25 加权融合，无 provider / 无 sqlite-vec 自动降级 FTS-only；mtime 增量索引 + embedding cache + 模型变更重建 + 单文件 chunk FIFO 上限 + CJK 逐字分词救 FTS 召回）+ `embeddings.py`（`OpenAICompatibleEmbeddingProvider` 复用 `llm.api_key`/`llm.base_url`；`MockEmbeddingProvider` 仅测试）+ `get_memory_manager` 单例 + `tools/builtin/memory_tools.py`（`memory_search`/`write_memory`/`read_memory`/`edit_memory` @tool，**模型驱动，不自动注入**——MemoryHook 注入的是使用策略 prompt，不是召回结果）+ `hooks/builtin/memory_hook.py`（`MemoryHook` priority 80，`before_model_call` 注入策略 prompt，空 store no-op）+ `config.yaml` `memory:` 块 + `permissions.tools` 默认 4 个 memory tool=allow + `pyproject` `[memory]` extra（sqlite-vec）。DB 落 `<WORKSPACE>/.twinkle_data/memory/memory.db`。spec `docs/superpowers/specs/2026-07-27-long-term-memory-design.md`。

**验收**：跨会话记住事实（如"用户偏好/项目约定"），新会话里 `recall` 注入相关记忆进上下文；无 embedding 配置时降级到 FTS 仍可用。 ✅

**仍 deferred**：5b 自动抽取（对话自动写记忆）、5c Dreaming（离线记忆整理，取代 FIFO 上限）。

---

### Phase 6 — 定时任务（cron）  `[已完成]`
**目标**：agent 能被定时唤醒执行任务，结果推送到通道。

**已落地**：`twinkle/gateway/cron/` 包（`CronSchedulerService` min-heap + croniter 算下一次执行时间，轮询 `cron_jobs.json` mtime 热加载；两阶段 wake→push：`wake_offset` 在 push 前先唤醒 agent，结果存 `CronRunState`，到 push 时间推到 targets 通道）+ `CronJobStore`（`<workspace>/cron_jobs.json` CRUD）+ `cron_models.py`（`CronJob`/`CronRunState`/`_Event`）+ `cron_expr.py`（croniter 解析）+ `twinkle/agentserver/tools/builtin/cron_tools.py`（5 个 @tool：`cron_list_jobs`/`cron_create_job`/`cron_update_job`/`cron_delete_job`/`cron_run_now`；`run_now` 写 sidecar 文件，gateway 检测后触发）+ `gateway/__main__.py` 启动时集成 `CronSchedulerService`。spec `docs/superpowers/specs/2026-07-29-cron-design.md`。

**验收**：注册一个 cron 任务，到点唤醒 agent 执行，结果推送到指定通道；支持单次任务与立即触发。 ✅

---

### Phase 7 — Skill 系统  `[已完成]`
**目标**：从"调原子工具"升级到"调用打包的知识+指令束（skill）"，支撑一类多步任务。

**已落地**：`twinkle/agentserver/skills/` 包（`Skill` + `SkillManager` 扫描/mtime 热重载/白名单 + `get_skill_manager` 单例）+ `tools/builtin/skill_tools.py`（`list_skill`/`read_skill` @tool）+ `hooks/builtin/skill_hook.py`（`SkillHook` priority 90，`before_model_call` 按 `TWINKLE_SKILL_MODE` 注入：`all`=每步注入清单 / `auto_list`=注入一句提示，默认 all）+ `<WORKSPACE>/skills/<name>/SKILL.md` 目录约定 + 示例 skill `doc-audit`（首次启动 seed）。`trigger` frontmatter 解析后丢弃（模型靠 description 自选，不做关键词自动匹配）。**SkillNet 一键下载/安装已落地**：前端「🧩 技能」页搜索 SkillNet 公开目录(api-skillnet.openkg.cn)并安装到 `<WORKSPACE>/skills`；搜索走 SkillNet 公开 API、下载走 GitHub Contents/raw API，自实现不依赖 skillnet-ai。spec `docs/superpowers/specs/2026-07-27-skill-design.md` + `docs/superpowers/specs/2026-07-30-skillnet-download-design.md`。

**验收**：一个打包 skill 能被 agent 选中并读入上下文指导多步任务执行；skill 与 builtin tool 协同。 ✅

**仍 deferred**：`skill_turbo` planner/executor、skill 进化（Phase 9）、marketplace/symphony（企业级）。

---

### Phase 8 — 子 Agent（subagent）  `[已完成]`
**目标**：从"单 agent 串行 ReAct"升级到"主 agent 可委派子 agent 并行/隔离执行子任务"，结果回灌由主 agent 整合。

**已落地**：`twinkle/agentserver/tools/builtin/subagent/` 包（`tools.py` 的 `spawn_subagent` @tool + `executor.py` 的 `SubagentExecutor` + `models.py` 的 `SubagentTaskSpec`/`SubagentResult`/`EXCLUDED_TOOLS` + `context.py` 的 ContextVar 隔离）+ `hooks/builtin/subagent_context_hook.py`（`SubagentContextHook` priority 50，`before_invoke` 设 executor + parent session/request id 到 ContextVar）。子 agent 即另开一个 `AgentLoop` 实例（复用 `LLMClient`/`SessionStore`，子 `ToolManager` 裁剪掉 `spawn_subagent`/`write_memory`/`edit_memory`），跑同一 `run_stream` 闭环，收敛后取 `e2a.complete` body 作 tool result。硬超时（300s）+ 软超时（120s 无流式响应）兜底；结果包 `[SYSTEM]` 停止提示防主 agent 重复委派；子 agent `max_steps=50`（更紧上限）。`build_agent_loop` 自动构建 `SubagentExecutor` + 织入 `SubagentContextHook`。spec `docs/superpowers/specs/2026-07-28-subagent-design.md`。

**验收**：主 agent 调 `spawn_subagent` 委派子任务 → 子 agent 独立跑完 ReAct 收敛 → 结果回灌 → 主 agent 总结给用户；超时/异常有兜底不挂死主循环。 ✅

**仍 deferred**：`fork_agent`（消息前缀继承）、流式转发（事件转发到父流）、skill 声明角色（`SubagentConfig` frontmatter）、多级嵌套与 team 编排。

---

### Phase 8a — Todo 增强  `[已完成]`
**目标**：将现有 Todo 系统从"扁平清单"增强为"结构化任务追踪"，对齐 jiuwenswarm 的 TodoItem + Claude Code 的 TaskCreate。

**已落地**：`twinkle/agentserver/todo/store.py`（`TodoTask` 数据模型增强：`id`(UUID)/`subject`/`description`/`blocked_by`/`owner`/`metadata`/`created_at`/`updated_at`；4 态：pending/in_progress/completed/cancelled；`TodoStore` API 重写：`create(subjects, sequential)`/`update(task_id, ...)`/`list(status?)`/`get(task_id)`）+ `twinkle/agentserver/tools/builtin/todo_tools.py`（4 个 @tool：`todo_create`/`todo_update`/`todo_list`/`todo_get`；`sequential=True` 一步创建线性依赖；轻量守卫：跳步检测 + 提醒，不拒绝）+ 前端 `TodoPanel.vue`（按状态分组 + 依赖/归属展示 + 脉冲动画）+ `webClient.ts`/`useSessions.ts` 类型更新。spec `docs/superpowers/specs/2026-08-01-todo-enhancement-design.md`。

**验收**：TodoTask 有唯一 ID、可设依赖、可追踪归属；`sequential=True` 一步创建线性依赖；前端按状态分组展示。 ✅

---

### Phase 9 — 上下文溢出恢复 + 重复调用检测  `[已完成]`
**目标**：agent 遇到上下文溢出时自动恢复，不再直接挂掉；agent 陷入重复工具调用循环时自动纠偏。

**已落地**：`twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`（`ContextOverflowRecoveryHook` priority 60，`on_model_exception` 检测 413/`context_length_exceeded`，3 层判定 + Anthropic/OpenAI 格式 token 解析，强制激进压缩（`keep_recent_pairs` 减半 + `threshold_override = limit_tokens × 0.85`）+ `request_retry()`，连续 3 次失败熔断注入 `[CONTEXT_OVERFLOW]` 消息）+ `twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py`（`RepeatToolCallDetectorHook` priority 88，`before_tool_call`/`after_tool_call`/`on_tool_exception` 记录 `(call_key, outcome_key)` 到 `deque(maxlen=30)` 滑动窗口，stable hash（SHA-256）+ 4 级分类（LOW: 同 call_key ≥ 10 → MEDIUM: A-B-A-B ≥ 10 → HIGH: 尾部连续相同 ≥ 20 → CRITICAL: ≥ 30），edge-triggered 只升不降，MEDIUM+ 自动注入 `[DETECTION]` 纠偏 system 消息，限频 5 次/分钟）+ `decorator.py` 改动（`ctx.extra["_tool_result"] = result` 传递 tool result 给 after-event hook）+ `config/schema.py` 新增 `OverflowRecoveryConfig` + `RepeatToolDetectionConfig`。spec `docs/superpowers/specs/2026-08-02-phase9-overflow-recovery-repeat-detection-design.md`。

**验收**：LLM 抛 413 时自动压缩重试成功；agent 连续 3 次调用相同工具时自动注入纠偏消息跳出循环。 ✅

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

### Phase 11 — PlanNode 递归执行树 + Fallback
**目标**：从"LLM 自驱的灵活规划"升级到"引擎驱动的可靠编排"，支撑固定流程的复杂任务（如 PPT 生成、深度研究报告）。

内容：
- **PlanNode 基类**：ABC，子类实现 `async _execute(inputs) -> Any`，`run()` 是模板方法（不可覆盖），自带 fallback。节点有 `plan_name` + `instruction` + `sub_plans`（递归树）。
- **能力注入**：节点通过回调访问 `call_tool` / `call_llm` / `stream_llm` / `extract_json`，不直接 import 系统模块。
- **安全沙箱**：`_SAFE_BUILTINS` 白名单 + `PlanCodeValidator` 代码校验，防止 skill 代码执行危险操作。
- **Fallback 机制**：节点失败自动触发 `fallback_callback`，不默默失败。`AbortError`（HITL 中断）不进 fallback，直接向上抛。
- **中间状态传递**：`inputs` dict 在节点间显式传递，上一个节点的输出直接成为下一个节点的输入，不依赖 LLM 上下文记忆。
- **对齐 jiuwenswarm**：`PlanNode`（`jiuwenclaw/agentserver/skill_turbo/plan_node.py`，enterprise_dev 当前 tip 已删除此文件）+ `RePlanExecutor` + `PlanCodeValidator`。

**范围控制**：先做 PlanNode 基类 + fallback + 安全沙箱 + 执行器；RePlan 的代码生成和验证（LLM 生成 plan code）列为紧随其后的第二步。

**验收**：定义一个 3 层 PlanNode 树，执行到中间节点失败时自动 fallback；节点间 inputs 正确传递；沙箱拒绝 import os/subprocess。

---

### Phase 12 — 中断恢复（对话历史驱动）
**目标**：agent 在任何中断（模型报错、用户停止、进程崩溃、审批中断）后，用户说「继续」或发新消息，LLM 能从对话历史自然恢复，不丢失任务上下文。

**设计理念**：对齐 Claude Code 的中断恢复模式——**对话历史就是状态**，不需要额外的 `DeepAgentState` 或 `Checkpointer`。LLM 读到中断标记就能理解发生了什么、从哪里继续。与 jiuwenswarm 的 `DeepAgentState` + `save_state/load_state`（为外层循环的循环变量设计）不同，Twinkle 当前是单轮 ReAct 架构，所有「状态」都在对话历史 + TodoStore + 审批文件中，不需要额外的状态持久化机制。

内容：
- **中断标记写入**：`run_stream` 的 `finally` 块中，如果请求不是正常完成（模型报错、异常中断、用户停止等），往 session 写一条 assistant 标记消息，包含中断原因和当前上下文。这样不管用户下次说什么，LLM 都能看到中断信息。
- **`_sanitize_orphan_tool_calls` 升级**：从 session 历史推导更丰富的中断上下文——不仅注入 `[interrupted]`，还包含：被中断的工具名和参数、当前 Todo 进度（从 TodoStore 读）、审批中断的 reason（从 `.approval_pending.json` 读）。
- **审批中断恢复**：进程崩溃后 `.approval_pending.json` 中的审批记录无法恢复 `asyncio.Future`。改为在 `_sanitize_orphan_tool_calls` 中读取审批记录，注入 `[interrupted: approval was pending (reason: ...)]`，让 LLM 重新决策（重新请求审批或换方案），而非尝试恢复 Future。
- **模型失败标记**：模型 API 报错/超时后，session 里只有 user 消息没有 assistant 回复。在 `except Exception` 的 `raise` 前往 session append 一条 assistant 标记 `[SYSTEM] 模型调用失败（...）`，避免 LLM 看到连续两条 user 消息而困惑。

**两条恢复路径**：
- **路径 A（正常中断）**：`run_stream` 的 `finally` 块能执行 → 实时写入中断标记。覆盖：模型报错、用户停止、Gateway 断连、审批中断。
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

### Phase 14 — Skill 自进化
**目标**：skill 定义能根据运行反馈自动改进。

内容：
- **轨迹 / 信号记录**：工具结果里的失败信号（`error|exception|失败|超时`）、用户纠正信号（`不对|应该`）；从读 `SKILL.md` 的 tool_call 反推当前活跃 skill。
- **evolve 闭环**：`detect → dedup → generate`（LLM 产 ≤2 条演进经验，带去重 + 优先级筛选）`→ approve → persist`（每个 skill 一个 `evolutions.json`）。
- **触发**：手动 `/evolve <skill>` 命令 + 每轮对话后自动 `run_auto_evolution`；`solidify` 把 pending 经验固化回 `SKILL.md` 本体。
- **前置依赖**：Phase 7 skill 系统 + Phase 5 长期记忆（经验库）。
- **范围控制**：复杂度高，先做信号检测 + 经验生成 + 手动审批；批量自动固化可后置。
- **对齐 jiuwenswarm**：`EvolutionRail`（`openjiuwen/harness/rails/evolution/evolution_rail.py`）+ `SkillEvolver`（`jiuwenclaw/evolution/evolver.py`）+ `SignalDetector`（`jiuwenclaw/evolution/signal_detector.py`）。

**验收**：跑失败的任务能产出 skill 演进经验，经审批固化回 `SKILL.md`，后续同类任务成功率提升。

---

### Phase 15 — MCP 工具接入
**目标**：让 twinkle 能挂载标准 MCP（Model Context Protocol）server 的工具，补足工具生态。

内容：
- 从 config 读 `mcp.servers`，转 `McpServerConfig`（stdio / sse transport）。
- 把 MCP server 暴露的工具注册进 `ToolManager`（复用现有 `schemas()` / `execute()` 面，agent_loop 零改动）。
- MCP 工具受 Phase 4 权限策略统一管控。
- **为何后置**：MCP 是纯扩展性 nice-to-have（builtin 工具已覆盖读写/搜索/执行），优先级低于让 agent 自主跑起来的能力。

**验收**：在 config 配一个 MCP server，agent 能像调 builtin 工具一样调其工具；权限策略对 MCP 工具同样生效。

---

### Phase 16 — DeepAgent 多轮外层循环 + 停止条件
**目标**：从"单轮 ReAct"升级到"多轮迭代直到任务完成"，支持复杂多步任务的可靠执行。

内容：
- **DeepAgent 外层循环**：包装现有 `AgentLoop`，在单次 `run_stream` 收敛后判断是否需要继续迭代。`LoopCoordinator` 跟踪迭代计数、token 预算、wall-clock 时间。
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

### Phase 18a — Team 编排 MVP（多 Agent 协作 · 阶段 A）

**目标**：1 leader + 2-3 角色化 member 并发执行子任务，leader 整合结果输出最终答案。

**设计原则**：Team 不是新执行引擎。ReActAgent step 循环提供多轮迭代，`_try_parallel_tool_calls` 提供并发，SubagentExecutor 是扩展点（加 role 参数 → 按 role 定制 system_prompt + 工具白名单）。Leader 调 `spawn_subagent(role="xxx")` 分派，成员通过共享 workspace 文件交换中间产物。

内容：
- **TeamConfig + MemberSpec**：YAML 驱动的成员角色定义（system_prompt + tools 白名单）。LLM 只传 role 名字，system_prompt/tools 由 executor 在服务端查 config。
- **SubagentExecutor 角色定制**：`execute_subagent` 按 role 查 `TEAM_MEMBERS`，覆盖子 agent 的 system prompt + 过滤 ToolManager。共享 workspace `team/<session>/shared/` 供成员交换文件。
- **Leader team 意识**：`build_system_prompt()` 条件追加团队协作段（角色清单 + 委派指南 + 共享路径）。
- **对齐 jiuwenswarm**：`TeamManager.build_agent_customizer()` 的成员特化逻辑（`jiuwenclaw/agentserver/team/team_runtime_inheritance.py`）+ `team_helpers.py` 的流式集成模式（`jiuwenclaw/agentserver/deep_agent/team_helpers.py`）。

**前置依赖**：Phase 8（subagent/spawn）+ Phase 16（多轮外层循环）。**注意**：PlanNode（Phase 11）与 Team 无关（PlanNode 是 skill 代码执行引擎，jiuwenswarm 源码零交叉引用），不作为前置。

**不做**：成员间直接通信、任务队列、Monitor 事件流、前端成员面板、team 级崩溃恢复。

**验收**：用户说 "写一份 AI safety 报告" → leader 并行 spawn researcher + writer → 产出到 team/shared/ → leader 整合输出。researcher 子 agent 调 write_file 被拒绝（不在白名单）。

**设计文档**：`docs/superpowers/specs/2026-08-05-team-collaboration-analysis.md` §4。

---

### Phase 18b — Team 编排完整版（多 Agent 协作 · 阶段 B）

**目标**：对齐 jiuwenswarm Team 六大维度——任务队列、成员通信、Monitor 事件流、崩溃恢复、前端面板。

内容：
- **TeamTaskStore**：任务队列 + 认领（claim）+ 完成/取消 + 依赖解除（基于 TodoStore 数据模型扩展）
- **MemberMessageBus**：P2P 消息 + Broadcast（在 ReActAgent 中注入 before_model_call hook 把消息 prepend 到 session）
- **TeamMonitorHandler**：从 member Event 收集 + 广播 14 种事件类型
- **新 E2A 帧类型 `e2a.team_event`**：Gateway 映射 + 前端 Team 面板（成员状态 / 任务进度 / 实时活动）
- **TeamRecoveryManager**：member 崩溃自动重启（Phase 12 中断标记 + per-member 孤儿 tool 清理）
- **对齐 jiuwenswarm**：`TeamManager`（`jiuwenclaw/agentserver/team/team_manager.py`）+ `RecoveryManager`（`openjiuwen/agent_teams/agent/recovery_manager.py`）+ `ReliabilityMonitor`（`openjiuwen/agent_teams/reliability/monitor.py`）。

**前置依赖**：阶段 A 跑通 + Phase 12（中断恢复）。

**验收**：3 个 agent 组成团队（研究员+写作员+审校员）→ 协作完成报告 → 一个成员崩溃后自动恢复 → 最终报告质量优于单 agent。

---

## 跨阶段基础设施

以下能力不在单一 Phase 中，而是随各 Phase 逐步积累形成的基础设施层：

### Hook 系统  `[已落地]`
Phase 4 引入最小钩子点后，逐步发展为完整的 Hook 框架：
- **`twinkle/agentserver/hooks/`** 包（`base.py` 的 `AgentHook` 基类 + `manager.py` 的 `HookManager` 优先级排序 + `decorator.py` 的 `@hook` 装饰器）
- **9 个 builtin hook**：
  - `PermissionHook`（priority 100，before_tool_call 权限拦截）
  - `ContextCompressionHook`（priority 95，before_model_call 自动压缩）
  - `SkillHook`（priority 90，before_model_call skill 注入）
  - `RepeatToolCallDetectorHook`（priority 88，before/after_tool_call 循环检测 + before_model_call 纠偏注入）
  - `MemoryHook`（priority 80，before_model_call 记忆策略注入）
  - `ContextOverflowRecoveryHook`（priority 60，on_model_exception 溢出恢复 + after_model_call 计数重置）
  - `SubagentContextHook`（priority 50，before_invoke ContextVar 桥接）
  - `LoggingHook`（priority 10，LLM/tool 调用日志）
  - `RetryHook`（priority 50，transient 异常自动重试）
- **事件**：`before_invoke`/`after_invoke`/`before_model_call`/`after_model_call`/`on_model_exception`/`before_tool_call`/`after_tool_call`/`on_tool_exception`
- **中断机制**：`HookInterrupt`（PermissionHook ASK 挂起/恢复）

### YAML 配置系统  `[已落地]`
- **`twinkle/config/`** 包（`schema.py` 的 pydantic 严格模型 + `loader.py` 的 YAML/env 加载）
- **优先级**：环境变量 > `.env` 文件 > `config.yaml` 默认值
- **配置块**：agentserver / gateway / workspace / logging / sessions / todos / llm / agent / context_compression / skills / memory / permissions / subagent
- spec `docs/superpowers/specs/2026-07-27-yaml-config-design.md`

### Web 工具  `[已落地]`
- **`web_fetch`**：httpx 异步抓取 + HTML→markdown + 长度截断 + Tavily extract fallback（anti-bot 403）
- **`web_search`**：Tavily 主力 + DDG fallback（无 key 时自动降级）+ max_results 控制

### 并行工具执行  `[已落地]`
- `agent_loop.py` 在同一 `tool_calls` 内多个工具调用时使用 `asyncio.gather` 并行执行，不串行等待

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
| M15 引擎驱动编排 | PlanNode 树 + fallback + 沙箱 | |
| M16 中断可恢复 | 中断标记 + `_sanitize_orphan_tool_calls` 升级 + 审批中断恢复 | |
| M17 文件可撤销 | 文件快照 + undo_file + file_history | |
| M18 skill 会进化 | 失败/纠正信号 → 经验固化回 SKILL.md | |
| M19 能挂外部工具 | MCP server 工具接入并受策略管控 | |
| M20 多轮迭代 | DeepAgent 外层循环 + 停止条件链 | |
| M21 深度研究 | 多步检索→分析→综合→报告 | |
| M22a 多 Agent 协作 MVP | leader + 2-3 角色化 member 并发委派 | |
| M22b 多 Agent 协作完整 | Team 编排 + 成员恢复 + 可靠性监控 | |
| M12 可观测 | OTel span 链 + 关键指标 | ✅ |

---

## 与 jiuwenswarm 参考实现的关系

- **学思想、借模式，不照搬依赖 openjiuwen 生态的实现**（manifest catalog / 分布式 swarm / symphony）。
- 每个 Phase 的"参考实现"锚点见对应小节；主分支源码仅 `.pyc`，`.py` 源码在 `enterprise_dev` 分支用 `git show enterprise_dev:<path>` 读取。
- 各模块对照见 `docs/architecture.md` §11；模块行为不清时查参考实现对应文件。
