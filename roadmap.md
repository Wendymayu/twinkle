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
- 工具系统（四层 ToolManager + `@tool` + builtin：web/shell/file/todo）
- 短期记忆（SessionStore 多轮对话记录）
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
- **OTel 遥测已落地**（`observability/` 包，启动接入，默认 off 零成本）：对应里程碑 M11 ✅。
- **Phase 3（长会话上下文压缩）**：初版已在 nightly worktree `nightly/phase-3-6-4` 实现（滑窗+LLM 摘要，独立模块 `context_compression.py`，不写回 SessionStore），待 review/merge 进主线；后续优化方向见 §Phase 3。
- **Phase 4 起为"后续必做"的进阶能力**（原属 deferred，现提升为规划项）。

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

### Phase 3 — 长会话上下文压缩  `[待启动]`
**目标**：长对话不爆 token、不丢关键上下文。

内容：
- 实现 `docs/en/ContextCompression.md` 对应能力：上下文压缩与卸载。
- 这是"短期记忆"里唯一需要后置的非平凡部分（对话记录本身已在 Phase 1）。

**初版**（nightly worktree `nightly/phase-3-6-4`，待 review/merge）：独立模块 `context_compression.py`（115 行），滑窗 + LLM 摘要，**不写回 SessionStore**（history 无损，只塑形 LLM 输入）；token 估算超阈值触发，保 head + 最近 tail（tool 配对不破），summary 失败降级为 head+tail。单模、仅主动、middle 丢弃不可召回。

**验收**：单会话 100 轮对话不爆 token、关键事实不丢。

**后续优化方向（对齐 jiuwenswarm）**——参考 `docs/en/ContextCompression.md` + `jiuwenclaw/agentserver/deep_agent/rails/context_overflow_recovery_rail.py`（`enterprise_dev` 分支，`git show enterprise_dev:<path>` 读取）：
- **rail 钩子织入**：压缩从 `run_stream` 内联提到框架切面（`before_model_call` / `on_model_exception` / `after_model_call`），与循环解耦，并为 Phase 5 记忆注入复用同一钩子点铺路。
- **413 反应救火重试**：LLM 抛 413 / `context_length_exceeded` 时解析 token 数（Anthropic / OpenAI / 华为三种格式），强压 + `request_retry()` 重试 + 连续失败熔断。初版只赌主动压缩能防住，赌错则请求直接挂。
- **触发条件多维度**：现仅 `estimate_tokens` 单阈值；加 message 计数维度、`large_message_threshold`（优先压大消息）、`offload_message_type`（可只压 tool 输出保对话）等旋钮。
- **窗口预算**：现固定阈值 60000；改为按模型窗口动态算（jiuwenswarm `threshold_override = 窗口 × 0.85`，预留 15% 给输出），随模型切换自适应。
- **异常降级增强**：现 summary 失败→丢 middle 保 head+tail；可加 offload 归档 + `[[OFFLOAD:...]]` 索引可检索召回（非丢弃），长会话早期被压事实能拉回。

---

### Phase 4 — 工具权限 / 审批 + 命令安全
**目标**：从"裸跑工具"升级到"工具可被策略管控，危险操作需审批"。

内容：
- **在 `agent_loop` 引入最小钩子点**（`before_tool_call` / `after_tool_call`）——后续权限、记忆注入（Phase 5）都挂在这上面。**不上完整 rail/plugin 系统**，只埋切面。
- **每工具档位策略**：`allow` / `deny` / `require-approval`。
- **危险工具交互审批流**（command_exec / write_file / edit_file）：`ASK` → 用户 `allow_once` / `allow_always` / `deny`，通过 channel 回调；决策结果与拒绝消息回灌 agent。
- **command_exec 安全增强**：在现有 blocklist + workspace 收敛基础上，借鉴 `bash_tool_safety.py` 补路径安全 / 预算控制。
- **审计日志** `ToolPermissionLog`：tool / decision(ALLOW/DENY/ASK) / source / rule / channel / session / timestamp，每决策都记。
- **精简范围（不做）**：shell AST 解析、三轴文件路径判定——复杂度高、对学习项目偏重，留作后续可选增强。`PERMISSION_ENABLED_CHANNELS` 按通道门控（其余通道全量放行）。

**验收**：危险工具调用前必须过策略；`require-approval` 工具触发用户审批卡；拒绝带 `[PERMISSION_DENIED]` 消息回灌；审计日志可查。

---

### Phase 5 — 长期记忆
**目标**：换掉 `memory.py` stub，agent 具备跨会话事实召回能力（RAG）。

内容：
- **embedding provider**：OpenAI-compatible（复用 `TWINKLE_LLM_BASE_URL` 体系）+ Mock fallback（无 key 时降级）。
- **召回存储**：单文件 SQLite 混合检索（`sqlite-vec` 向量余弦 + FTS5 BM25，`strictest` 合并），无 embedding 配置时自动降级 FTS-only。DB 落在 `~/.twinkle/memory/memory.db`。
- **`recall()` / `store()` 真实现**：在 agent loop 每轮 `llm.stream` 前注入召回的相关记忆（借 Phase 4 的钩子点）。
- **接口形状不变**（Phase 1 已钉死），不回炉。
- **不做**：外部记忆 provider 分发（mem0/openviking）、wiki LLM 子 agent 索引——留作后续可选。

**验收**：跨会话记住事实（如"用户偏好/项目约定"），新会话里 `recall` 注入相关记忆进上下文；无 embedding 配置时降级到 FTS 仍可用。

---

### Phase 6 — 定时任务（cron）
**目标**：agent 能被定时唤醒执行任务，结果推送到通道。

内容：
- **`CronSchedulerService`**：min-heap + `croniter` 算下一次执行时间，轮询 `cron_jobs.json` 的 mtime 做热加载。
- **两阶段 wake→push**：`wake_offset`（默认 300s）在 push 前先唤醒——构造 `E2AEnvelope`（`channel_id="__cron__"`、`session_id="cron_<ts>_<jobid>"`、`method=chat.send`、`params.content=job.description`）发给 AgentServer；结果存 `CronRunState`，到 push 时间推到 `targets` 通道。
- **`CronController`**：给 WebChannel 的单例 CRUD API（create/update/delete/toggle/list/preview）；支持单次任务（`delete_after_run`）、过期标记、`trigger_run_now` 立即触发。
- **无前置依赖**：不依赖权限/记忆/skill，可独立落地（故提前到 Phase 6，让 agent 尽早具备定时自主能力）。

**验收**：注册一个 cron 任务，到点唤醒 agent 执行，结果推送到指定通道；支持单次任务与立即触发。

---

### Phase 7 — Skill 系统
**目标**：从"调原子工具"升级到"调用打包的知识+指令束（skill）"，支撑一类多步任务。

内容：
- **skill 定义**：`SKILL.md`（`name` / `description` frontmatter + 指令 + 示例），比 tool 高一层的抽象（一个 skill 包多个 tool + 流程）。
- **skill 注册 / 发现 / 检索**：`SkillManager` 扫描 skills 目录，`ENABLED_SKILLS` 冷启动白名单；agent 决定何时把哪个 `SKILL.md` 读进上下文。
- **参考实现**：`jiuwenclaw/agentserver/skill_manager.py` + `skill_turbo/`（planner→executor→fallback DeepAgent）。
- **范围控制**：先做 skill 加载 + 选择注入；planner/executor 子 agent 编排可后置。

**验收**：一个打包 skill 能被 agent 选中并读入上下文指导多步任务执行；skill 与 builtin tool 协同。

---

### Phase 8 — Skill 自进化
**目标**：skill 定义能根据运行反馈自动改进。

内容：
- **轨迹 / 信号记录**：工具结果里的失败信号（`error|exception|失败|超时`）、用户纠正信号（`不对|应该`）；从读 `SKILL.md` 的 tool_call 反推当前活跃 skill。
- **evolve 闭环**：`detect → dedup → generate`（LLM 产 ≤2 条演进经验，带去重 + 优先级筛选）`→ approve → persist`（每个 skill 一个 `evolutions.json`）。
- **触发**：手动 `/evolve <skill>` 命令 + 每轮对话后自动 `run_auto_evolution`；`solidify` 把 pending 经验固化回 `SKILL.md` 本体。
- **前置依赖**：Phase 7 skill 系统 + Phase 5 长期记忆（经验库）。
- **范围控制**：复杂度高，先做信号检测 + 经验生成 + 手动审批；批量自动固化可后置。

**验收**：跑失败的任务能产出 skill 演进经验，经审批固化回 `SKILL.md`，后续同类任务成功率提升。

---

### Phase 9 — MCP 工具接入
**目标**：让 twinkle 能挂载标准 MCP（Model Context Protocol）server 的工具，补足工具生态。

内容：
- 从 config 读 `mcp.servers`，转 `McpServerConfig`（stdio / sse transport）。
- 把 MCP server 暴露的工具注册进 `ToolManager`（复用现有 `schemas()` / `execute()` 面，agent_loop 零改动）。
- MCP 工具受 Phase 4 权限策略统一管控。
- **为何后置**：MCP 是纯扩展性 nice-to-have（builtin 工具已覆盖读写/搜索/执行），优先级低于让 agent 自主跑起来的 cron，故与 cron 换序后置。

**验收**：在 config 配一个 MCP server，agent 能像调 builtin 工具一样调其工具；权限策略对 MCP 工具同样生效。

---

## 里程碑

| 里程碑 | 验收标准 | 状态 |
|---|---|---|
| M1 两进程通 | ws echo 贯穿 web↔gateway↔agentserver | ✅ |
| M2 能调工具 | 真模型 + 只读工具闭环 + 多轮上下文 | ✅ |
| M3 能管工具 | 多工具选择 + 任务规划 | ✅ |
| M4 能扛长会话 | 100 轮不爆 token、不丢关键事实 | ⏳ |
| M5 工具可管控 | 危险工具审批 + 命令安全 + 审计日志 | |
| M6 有长期记忆 | 跨会话事实召回 + RAG 注入 | |
| M7 会定时跑 | cron 唤醒 agent + 结果推送通道 | |
| M8 能用 skill | skill 加载 / 选择 / 注入指导多步任务 | |
| M9 skill 会进化 | 失败/纠正信号 → 经验固化回 SKILL.md | |
| M10 能挂外部工具 | MCP server 工具接入并受策略管控 | |
| M11 可观测 | OTel span 链 + 关键指标 | ✅ |

---

## 与 jiuwenswarm 参考实现的关系

- **学思想、借模式，不照搬依赖 openjiuwen 生态的实现**（manifest catalog / 分布式 swarm / symphony）。
- 每个 Phase 的"参考实现"锚点见对应小节；主分支源码仅 `.pyc`，`.py` 源码在 `enterprise_dev` 分支用 `git show enterprise_dev:<path>` 读取。
- 各模块对照见 `docs/architecture.md` §11；模块行为不清时查参考实现对应文件。
