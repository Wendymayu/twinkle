# Twinkle — 个人助手 Roadmap

## 0. 定位与决策

- **性质**：学习型重写项目。参考并重写 [jiuwenclaw](D:/opensource/gitcode/jiuwenclaw) 的**核心 agent 链路**，不是 fork、不是通用 SaaS 套壳。
- **主架构**：保留 gateway ↔ agentserver 两进程 + 双向 WebSocket，对齐 jiuwenclaw 主架构，降低后续与参考实现的差距。理由是学习对照，不是 cargo-cult。
- **技术栈**：Python。
- **用户通道**：当前只 Web，channel 适配层预留（后续可接钉钉/飞书，但 jiuwenclaw 已有实现，到时参考即可）。

### 明确不做（砍）
- Skill 系统（全砍——已核实 `tool_manager` 不硬依赖 `skill_manager`，可安全砍）
- Skill 自进化（`evolution/`）
- 多 channel（只留 web，砍 dingding/feishu/wechat/discord/...）
- 企业级特性（`tenant_agent_pool`、`sts_service`、`telemetry`、`cron/heartbeat` 定时任务）
- 长期记忆（用户偏好、经验、RAG/wiki/embedding——`memory/` 里的 `embeddings.py`/`wiki_manager.py`/`external_memory_builder.py` 全不做）
- 危险工具审批与审计（`permissions/`，先不做）

### 明确保留
- gateway↔agentserver 两进程 + 双向 ws
- agent loop 核心闭环
- 工具系统（tool_manager + 动态注册 + 任务规划）
- 短期记忆（对话记录 + 长会话上下文压缩）
- 单 web channel

---

## 阶段

### Phase 0 — 两进程骨架打通
**目标**：把 gateway↔agentserver 两进程 + 双向 ws + 单 web channel 跑通一条 echo。

内容：
- gateway 进程、agentserver 进程，各自启动。
- 双向 ws，定义消息信封（路由 / 会话 id / 流式分片）。
- web channel 接入 gateway（channel 适配层接口先埋，实现只 web 一种）。
- **关键命门**：先把 jiuwenclaw 的 gateway/server 接缝搞清——`gateway_push/`（server 侧主动推回 gateway）与 `agent_client.py`（gateway 侧调 server）的职责边界。Phase 0 必须定义清楚哪边负责什么，否则两进程拆分没有意义。

**验收**：浏览器发消息 → gateway ws → server ws → server 流式回 → 前端逐字显示。这条贯穿通是 Phase 0 唯一验收标准，没打通不进下一阶段。

---

### Phase 1 — agent loop 最小闭环 + 短期对话记忆
**目标**：接真模型，跑通 think→选工具→执行→结果回灌→再决策；同时落短期记忆（多轮对话记录）。

内容：
- **直接接真模型，不做 mock 阶段**。
- agent loop 最小闭环；1~2 个只读工具（如读本地文件、查天气）。
- **短期记忆落地**：session 对话记录、多轮上下文。这是 agent loop 多轮的必需件，不后置。
- **长期记忆用 stub**：jiuwenclaw 里 `agent_manager` 调用长期 memory 的接口先空实现，埋好接口形状，后续不回炉。

**验收**：用户多轮提问 → 模型在跨轮上下文中正确判断是否调只读工具 → 调用 → 结果整合进回答 → 跨轮记住上文。

---

### Phase 2 — 工具系统成形
**目标**：从"能调一个工具"升级到"能管理一批工具并规划任务"。

内容：
- tool_manager：动态工具注册、工具目录（`tool_catalog`）。
- 任务规划：`plan_todo_context`，把多步任务拆解、跟踪。
- tool 并发控制（`tool_concurrency`）——倾向先砍，单用户串行够用；如 Phase 2 出现并发需求再补。

**验收**：多工具正确选择 + 多步任务规划链路可用。

---

### Phase 3 — 长会话上下文压缩
**目标**：长对话不爆 token、不丢关键上下文。

内容：
- 实现 `docs/en/ContextCompression.md` 对应能力：上下文压缩与卸载。
- 这是"短期记忆"里唯一需要后置的非平凡部分（对话记录本身已在 Phase 1）。

**验收**：单会话 100 轮对话不爆 token、关键事实不丢。

---

## 里程碑

| 里程碑 | 验收标准 |
|---|---|
| M1 两进程通 | ws echo 贯穿 web↔gateway↔agentserver |
| M2 能调工具 | 真模型 + 只读工具闭环 + 多轮上下文 |
| M3 能管工具 | 多工具选择 + 任务规划 |
| M4 能扛长会话 | 100 轮不爆 token、不丢关键事实 |

## 4. 后续（明确推迟，不在当前范围）

长期记忆（用户偏好/经验/RAG/wiki）、skill 系统、多 channel、危险工具审批、定时任务、企业级特性。需要时参考 jiuwenclaw 对应模块实现。
