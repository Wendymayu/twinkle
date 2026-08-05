# Twinkle Agent Team 协作：差距分析与实现路径

> 日期：2026-08-05 · 状态：分析文档，待 review
> 对齐：jiuwenswarm `TeamAgent`（openjiuwen SDK）+ `TeamManager`（jiuwenclaw 薄封装）+ `team_helpers.py`（流式集成）
> 前因：用户询问"当前项目是否具备实现 agent team 协作的基础"，经源码级分析发现 roadmap 中 Phase 11(PlanNode)→Phase 18 的依赖关系不反映 jiuwenswarm 真实架构，故先写正分析再出方案。

---

## 0. 核心发现：roadmap 前置依赖链有两处需要修正

### 0.1 PlanNode 跟 Team 无关

全仓库搜索（`enterprise_dev` 分支）只有**一处** `PlanNode` 定义：

```
jiuwenclaw/agentserver/skill_turbo/plan_node.py
```

它在 `skill_turbo` 包里，是 **skill 代码执行引擎**（让 skill 定义确定性 `_execute` 图），Team 模块零 import。roadmap 写 "Phase 11（PlanNode）→ Phase 18（Team）" 是 Twinkle 自己的设计假设，不是 jiuwenswarm 的实际架构。

### 0.2 jiuwenswarm Team 的实际编排原语

Team 编排的核心是一行 SDK 调用（`team_helpers.py:238`）：

```python
async for chunk in Runner.run_agent_team_streaming(
    agent_team=team_agent,
    inputs={"query": initial_query},
    session=session_id,
):
```

`TeamAgent`（openjiuwen SDK，源码不在本分支）内部靠这些原语运作：

| 原语 | 做什么 |
|---|---|
| **Task Queue** | 任务 create → claim → complete → cancel → unblock |
| **Member Lifecycle** | spawned → ready → busy → paused → error → restarted → shutdown |
| **Event Bus** | P2P 消息 + Broadcast + Monitor 事件流（14 种事件类型） |
| **Shared State** | SQLite 持久化的 team state + shared DB |
| **Member DeepAgent** | 每个 member 是独立 `DeepAgent` 实例（有自己的 ReAct loop、tools、skills） |

jiuwenclaw 层只是薄封装：

| jiuwenclaw 文件 | 行数 | 实际职责 |
|---|---|---|
| `team_manager.py` | 729 | Session 级的 create/destroy/interact 生命周期 |
| `team_helpers.py` | ~320 | 把 TeamAgent 的 stream 接回 AgentResponseChunk 响应流 |
| `config_loader.py` | ~210 | YAML 配置 → `TeamAgentSpec` dict |
| `monitor_handler.py` | ~280 | `TeamMonitor` 事件 → 前端可消费格式 |
| `team_runtime_inheritance.py` | ~180 | 给 member 装 rails + 继承 tools |
| `member_subagents.py` | 24 | 把主 DeepAgent 的 subagent spec 复制给 member |
| `event_types.py` | ~130 | 14 种事件类型枚举 |
| `exceptions.py` | ~50 | 7 个 Team 异常类 |

### 0.3 修正后的真正硬前置

| 前置 | 状态 | 为什么需要 |
|---|---|---|
| **subagent/spawn**（Phase 8） | ✅ 已落地 | 每个 team member 创建一个隔离的 AgentLoop，`spawn_subagent` 已证明可行 |
| **多轮外层循环**（Phase 16） | ❌ 未做 | 当前 `run_stream` 收敛（`finish_reason="stop"`）就结束。Team leader 需要 "分派任务 → 等 member 完成 → 审视 → 继续迭代" 的外层包装 |
| **中断恢复**（Phase 12） | ✅ 已落地 | member 崩溃后从对话历史恢复，不拖垮整个 team |
| ~~PlanNode~~（Phase 11） | ❌ 且不需要 | 跟 Team 无关，管的是 skill 代码执行 |

---

## 1. jiuwenswarm Team 的六大维度

逆向分析 jiuwenclaw + openjiuwen SDK 调用链后，Team 完整能力拆为六维：

### 1.1 多 Agent 生命周期

`TeamAgent.build()` 从 `TeamAgentSpec` 创建 leader + N 个 member。每个 member 是独立 `DeepAgent` 实例，有 8 个状态：

```
UNSTARTED → READY → BUSY → PAUSED → STOPPED → ERROR → RESTARTING → SHUTDOWN
```

`TeamManager` 管理 session 级的 team 实例（`_team_agents: dict[session_id, TeamAgent]`），同 session 内复用。

### 1.2 任务分发

- Leader 创建 task → 放入 team 的 task queue
- Member 认领（claim）task → 执行 → 完成（complete）或取消（cancel）
- Task 有依赖关系：A 被 B 阻塞 → B 完成后 A 自动 unblock
- 通过 `MonitorEvent` 广播（`TASK_CREATED` / `TASK_CLAIMED` / `TASK_COMPLETED` / `TASK_CANCELLED` / `TASK_UNBLOCKED`）

### 1.3 成员间通信

- **P2P 消息**：一个 member 给另一个 member 发定向消息
- **Broadcast**：leader 给全体发广播
- **共享 Workspace**：team 有共享文件目录（`team_home(team_name) / "team-workspace"`），成员可读写中间产物

### 1.4 运行时监控

`TeamMonitorHandler` 从 `TeamMonitor` 消费 14 种事件：

| 事件类别 | 事件类型 |
|---|---|
| 成员事件 | `MEMBER_SPAWNED` / `MEMBER_STATUS_CHANGED` / `MEMBER_EXECUTION_CHANGED` / `MEMBER_RESTARTED` / `MEMBER_SHUTDOWN` |
| 任务事件 | `TASK_CREATED` / `TASK_CLAIMED` / `TASK_COMPLETED` / `TASK_CANCELLED` / `TASK_UNBLOCKED` |
| 消息事件 | `MESSAGE_P2P` / `MESSAGE_BROADCAST` |
| 活动事件 | `MEMBER_TOOL_CALL` / `MEMBER_TOOL_RESULT` |

事件广播到所有等待的 request queue（`_pending_waiters`），前端多个连接都能收到。

### 1.5 崩溃恢复

`RecoveryManager`（openjiuwen SDK）从 SQLite 持久化状态恢复崩溃的 member：
- 重启 member `DeepAgent`
- 从 task queue 中恢复未完成的任务
- `TeamAgent.destroy_team(force=True)` 做清理

`team_manager.py` 的 `_cleanup_team_runtime_state()` 在每次 create 前清理上次残留的运行时表。

### 1.6 成员特化

`build_agent_customizer()` 给每个 member 定制：

- **Rails**：白名单控制（`RAIL_WHITELIST` 15 个 rail，如 RuntimePromptRail / SecurityRail / SubagentRail / SkillUseRail 等）
- **Tools**：从 leader 继承，白名单控制（`TOOL_WHITELIST` 50+ 个工具，如 web_search / fetch_webpage / task_tool / user_todos 等）
- **Skills**：全局 skills 先拷到 team 共享目录，再按 member 配置分发给各自的 skills 目录
- **Model**：每个 member 可配独立模型（或继承默认）
- **Workspace**：每个 member 有独立工作区（`stable_base=True` 即共享 team workspace）
- **Subagent**：member 也可有 subagent（`assign_team_member_subagents` 复制主 agent 的 subagent spec）

---

## 2. Twinkle 现状 —— 有什么

### 2.1 subagent 基础（Phase 8）

`spawn_subagent` 已证明以下能力：

- 创建隔离的 `AgentLoop` 实例（新 session、裁剪 ToolManager）
- ContextVar 隔离（父/子不互相污染）
- 软硬超时保护
- 黑盒结果回灌（`{role:"tool"}` + `_SUBAGENT_STOP_HINT`）

**限制**：一次只跑一个子 agent（串行在 `agent_loop` 的 `for tc in tcs:` 里），跑完就结束。

### 2.2 AgentLoop ReAct 闭环

每个 member 的执行引擎可直接复用现有 `AgentLoop`（和子 agent 一样）：

- LLM 驱动 think → tool → result → re-decide
- ToolManager 提供能力边界
- Hook 系统提供行为注入（prompt / 压缩 / 权限 / skill / memory）
- SessionStore 提供持久化（对话历史 + 元数据）

### 2.3 Hook 系统（9 个 builtin hook）

| Hook | 对 member 的意义 |
|---|---|
| `SkillHook` | member 可用 skill |
| `MemoryHook` | member 可检索长期记忆 |
| `ContextCompressionHook` | member 长上下文不爆 |
| `LoggingHook` | member 调用日志 |
| `RetryHook` | member 失败自动重试 |
| `PermissionHook` | member 工具受控（如果挂的话） |
| `RepeatToolCallDetectorHook` | member 陷入循环自动纠偏 |
| `ContextOverflowRecoveryHook` | member 413 自动恢复 |
| `SubagentContextHook` | member 也可委派子 agent |

### 2.4 其他基础设施

| 能力 | 对 Team 的意义 |
|---|---|
| ToolManager + `@tool` | 每个 member 可有独立工具集 |
| Skill 系统 | per-member 技能加载 |
| Memory 系统（SQLite 向量混合检索） | team 共享或独立长期记忆 |
| Todo 系统（`TodoTask` + id/owner/blocked_by/status） | 可扩展为 team task store |
| SessionStore 持久化 | member 会话落盘 + 崩溃恢复 |
| E2A 协议 + Gateway | team 事件可通过新帧类型推送前端 |
| OTel 遥测 | per-member span 追踪 |

---

## 3. 差距逐层分析（6 层 → 缺什么）

### 🔴 Layer 1 — 多 Agent 并发 + 外层循环（核心缺口）

**当前**：`spawn_subagent` 在父 `agent_loop` 的工具调用循环里**串行**执行；父 LLM 说 `stop` 后整个 `run_stream` 就结束了。

**需要**：
- 并发执行器：同一轮多个 member 同时跑，不是等一个跑完再跑下一个
- 外层循环：leader 的一次 `run_stream` 收敛不代表 team 任务完成——需要 "审视结果 → 分派下一轮 → 继续" 的包装

**工作量**：最大。没有这一层，Team 就是单次委派后就停的"一次性 subagent"，不是 "持续协作的 team"。

### 🟠 Layer 2 — 任务队列与分发

**当前**：Todo 系统有 `id/owner/blocked_by/status` 字段，但它是 agent **自己规划**的任务（agent 调 `todo_create`），不是 leader **分派**给 member 的任务。

**需要**：
- Team task store：task 有 assignee（哪个 member）、依赖（被哪个 task 阻塞）、优先级
- Task routing：leader 说 "研究员做 X、写作员做 Y"
- Task unblock：A 完成后自动通知 "B 的阻塞解除了"

**工作量**：中等。可在现有 `TodoStore` 上扩展（加 team 级路由和 assignee 字段），也可新建 `TeamTaskStore`。

### 🟠 Layer 3 — 成员间通信

**当前**：子 agent 完全黑盒，中间帧全部丢弃，只回 final 字符串。

**需要**：
- P2P 消息：member A 对 member B 说 "你给的数据不够，再深挖"
- Broadcast：leader 对全体说 "方向错了，改做 Y"
- 共享上下文：member 产出的中间文件/notes 可被其他 member 读取

**工作量**：中等偏大。需要在 `run_stream` 中增加"接收消息"机制（当前纯 push），以及消息路由层。

### 🟡 Layer 4 — 运行时监控与事件流

**当前**：子 agent 的中间帧（chunk/todo_update）全部丢弃，前端不可见。

**需要**：
- 新 E2A 帧类型：`e2a.team_event`（team 侧信道，类似 `e2a.todo_update`）
- 事件类型：member 状态变化 / 任务进度 / 工具调用 / 工具结果
- 前端 UI：成员面板（状态 + 活动 + 产出）

**工作量**：中等。E2A 协议扩展 + 前端新面板。

### 🟡 Layer 5 — 崩溃恢复（Team 级）

**当前**：Phase 12 已做单 agent 的中断恢复（中断标记 + 孤儿 tool_call 清理）。

**需要**：
- Member 级恢复：member_2 崩溃 → leader 感知 → 重启或重新分派任务
- 团队不雪崩：一个 member 挂不影响其他 member
- Team state 持久化：哪些 member 在做哪些 task，跨重启可恢复

**工作量**：中等。Phase 12 打了基础，扩展为 per-member 即可。

### 🟢 Layer 6 — 成员角色特化（相对容易）

**当前**：子 agent 只有一个硬编码 system prompt（`_SUBAGENT_ADDENDUM`）。

**需要**：
- Per-member persona：研究员 / 写作员 / 审校员各有不同 system prompt
- Per-member tool set：研究员有 web_search/web_fetch，写作员有 file_tools，审校员只读
- Per-member skill：研究员有 research skill，写作员有 writing skill

**工作量**：较小。大部分是配置驱动的 prompt 定制 + 工具白名单（现有 `EXCLUDED_TOOLS` 模式改 per-member 白名单即可）。

---

## 4. 实现路径：两阶段

### 4.1 阶段 A：简易 Team（MVP）

**目标**：1 个 leader + 2-3 个 member 能并发执行子任务，leader 整合结果输出最终答案。

**不做**：成员间通信、任务队列、Monitor 事件流、崩溃恢复、前端成员面板。

**实现要点**：

1. **Leader 的外层循环**（最简版 Phase 16）：
   - 给 `AgentLoop` 加一个 `max_rounds` 参数（默认 1，即当前行为）
   - `max_rounds > 1` 时，`run_stream` 在 `finish_reason="stop"` 后不立即返回，而是检查：还有 pending team tasks 吗？有就继续下一轮
   - 收敛条件：所有 member task 完成 + leader LLM 输出 stop（或撞 max_rounds / token 预算）

2. **并发 member 执行**：
   - `spawn_subagent` 改为可并发：一轮 tool_call 中多个 spawn 用 `asyncio.gather` 并发执行
   - 或者在 leader loop 上层加一个并发调度器：收集本轮要 spawn 的 tasks → 并发执行 → 结果回灌 → 下一轮

3. **Per-member 特化**（配置驱动）：
   ```yaml
   team:
     members:
       researcher:
         system_prompt: "你是研究员..."
         tools: [web_search, web_fetch, read_file]
         skills: [deep-research]
       writer:
         system_prompt: "你是写作员..."
         tools: [read_file, write_file]
         skills: [writing-guide]
   ```
   - `spawn_subagent` 的 `SubagentTaskSpec` 加 `role` 字段 → `SubagentExecutor` 按 role 查找配置 → 应用 per-member system_prompt + tools + skills

4. **共享 Workspace**：
   - 子 agent workspace 独立（`<workspace>/team/<team_id>/<member>/`），防止互相覆盖文件
   - Leader 可读所有 member 的产出物

5. **新 E2A 帧类型**：`e2a.team_event`（最小版，只含 member 状态变化和最终结果）

**验收**：
1. 用户说 "写一份关于 AI safety 的报告" → leader 创建 2 个 member（研究员+写作员）→ 研究员查资料 → 写作员写报告 → leader 整合输出
2. 一个 member 超时 → 不影响其他 member → leader 看到失败信息并调整策略
3. member 的中间工具调用在前端可见（team 面板）

### 4.2 阶段 B：完整 Team

**目标**：对齐 jiuwenswarm Team 的六大维度。

**内容**：
- 任务 Queue + 认领/完成/依赖解除
- 成员间 P2P + Broadcast 通信
- 完整 Monitor 事件流（14 种事件类型）
- 崩溃恢复 + 成员自动重启
- 前端 Team 面板（成员状态 / 任务进度 / 实时活动）
- Config 驱动的成员模板注册表
- Team state 持久化（SQLite）

**前置条件**：阶段 A 跑通 + 前端团队资源。

---

## 5. 阶段 A 详细设计

### 5.1 外层循环：`TeamLoop`

新增 `twinkle/agentserver/team_loop.py`：

```python
class TeamLoop:
    """包装 AgentLoop，支持多轮 team 编排。
    
    每轮：leader ReAct → 收敛（stop 或 tool_calls）
    - 如果收敛且有 pending member tasks → 收集结果 → 喂回 leader → 下一轮
    - 如果没有 pending tasks → 返回最终答案
    - 撞 max_rounds → 强制收敛
    """

    def __init__(
        self,
        leader_loop: AgentLoop,
        member_registry: dict[str, MemberConfig],
        max_rounds: int = 10,
        token_budget: int | None = None,
    ):
        ...

    async def run_stream(self, env: E2AEnvelope) -> AsyncIterator[E2AResponse]:
        """Team 外层循环：多轮迭代直到收敛。"""
        round_idx = 0
        pending_tasks: dict[str, TeamTask] = {}

        while round_idx < self._max_rounds:
            round_idx += 1
            # 跑一轮 leader ReAct
            leader_final = None
            spawned_this_round: list[TeamTask] = []

            async for frame in self._leader_loop.run_stream(env):
                if frame.response_kind == "e2a.chunk":
                    yield frame  # 透传 leader 文字流
                elif frame.response_kind == "e2a.todo_update":
                    yield frame
                elif frame.response_kind == "e2a.complete":
                    leader_final = frame
                # spawn_subagent 的 tool_result 已被回灌到 leader session

            # 检查是否有并发 member tasks 需要跑
            if spawned_this_round:
                results = await asyncio.gather(*[
                    self._run_member(task) for task in spawned_this_round
                ])
                # 结果回灌进 leader session（作为 tool_result）
                for task, result in zip(spawned_this_round, results):
                    self._inject_member_result(env.session_id, task, result)
                    yield self._team_event(task.member_id, "completed", result)
                continue  # 下一轮 leader 看到结果后继续

            # 没有新 member tasks → 真正收敛
            if leader_final:
                yield leader_final
                return

        # 撞 max_rounds
        yield self._error_frame("max_rounds reached")
```

**关键设计决策**：

- Leader 仍是普通 `AgentLoop`（不改造），`TeamLoop` 是外层包装
- Member tasks 通过 leader 调 `spawn_subagent` 触发 → 被 `TeamLoop` 拦截，改为并发执行
- 或者更简单：在 Team 模式下，`spawn_subagent` 的参数里带 `role`，`SubagentExecutor` 按 role 应用配置

### 5.2 并发 member 执行

改造 `agent_loop.py` 的工具调用循环（`_inner_run_stream` 的 `for tc in tcs:` 部分）：

```python
# 当前（串行）
for tc in tcs:
    result = await self._tools.execute(tc.name, tc.args)

# 改为（可选并发）
spawn_calls = [tc for tc in tcs if tc.name == "spawn_subagent"]
other_calls = [tc for tc in tcs if tc.name != "spawn_subagent"]

# 先并发跑所有 spawn_subagent
spawn_results = await asyncio.gather(*[
    self._tools.execute(tc.name, tc.args) for tc in spawn_calls
], return_exceptions=True)

# 再串行跑其他工具（保持确定性）
for tc in other_calls:
    result = await self._tools.execute(tc.name, tc.args)
```

**为何只 spawn_subagent 并发**：其他工具有副作用（文件写、命令执行），并发顺序不确定 → 文件冲突风险。`spawn_subagent` 是纯隔离的 → 天然可并发。

### 5.3 Per-member 配置

`config.yaml` 新增 `team:` 块：

```yaml
team:
  enabled: false  # 默认关，和 permissions 一样 opt-in
  max_rounds: 10
  members:
    researcher:
      system_prompt: |
        You are a research specialist. Your job is to find, analyze,
        and synthesize information. Use web_search and web_fetch to
        gather data. When done, return a structured research brief.
      tools: [web_search, web_fetch, read_file, memory_search, read_memory]
      skills: []
      model: ""  # "" = 复用 leader 模型
    writer:
      system_prompt: |
        You are a writing specialist. Your job is to produce clear,
        well-structured documents. Use file tools to write drafts.
        Read research briefs from the shared workspace, do NOT do
        your own research.
      tools: [read_file, write_file, edit_file]
      skills: [writing-guide]
```

`SubagentTaskSpec` 扩展：

```python
class SubagentTaskSpec(BaseModel):
    objective: str
    prompt: str = ""
    role: str = ""  # 新增：对应 team.members 的 key
```

`SubagentExecutor.execute_subagent` 按 `role` 查找配置，覆盖 system_prompt / tools。

### 5.4 共享 Workspace

```
<WORKSPACE>/.twinkle_data/team/<team_session_id>/
    shared/               # leader/members 共享读写
    researcher/           # researcher 的工作区
    writer/               # writer 的工作区
```

- Member 的 `file_tools` 根目录设为其独立 workspace
- 读 `shared/` 时转调 `read_file`（从 team shared 路径读）
- Leader 的 workspace 指向 team 根目录，可遍历所有 member 产出

### 5.5 Team 事件帧

新 E2A 响应类型 `e2a.team_event`（对齐 `e2a.todo_update` 的侧信道模式）：

```json
{
  "response_kind": "e2a.team_event",
  "is_final": false,
  "body": {
    "event_type": "team.member.completed",
    "member_id": "researcher",
    "status": "completed",
    "summary": "Research completed: 3 sources analyzed"
  }
}
```

Gateway `MessageHandler._process_stream` 映射为浏览器 event：

```json
{
  "type": "event",
  "event": "team.update",
  "payload": { ... }
}
```

### 5.6 文件清单

**新增**：
- `twinkle/agentserver/team/__init__.py` — re-exports
- `twinkle/agentserver/team/team_loop.py` — `TeamLoop` 外层循环
- `twinkle/agentserver/team/config.py` — `TeamConfig` + `MemberConfig` pydantic 模型
- `twinkle/agentserver/team/workspace.py` — team workspace 管理（创建/清理子目录）
- `twinkle/agentserver/tools/builtin/subagent/models.py` — `SubagentTaskSpec` 加 `role` 字段
- `twinkle/agentserver/tools/builtin/subagent/executor.py` — `execute_subagent` 按 role 应用配置

**改动**：
- `twinkle/agentserver/agent_loop.py` — 工具调用循环 spawn_subagent 并发化
- `twinkle/config/schema.py` — 新增 `TeamConfig` + `MemberConfig`
- `twinkle/resources/config.yaml` — 新增 `team:` 块
- `twinkle/e2a/models.py` — `response_kind` 新增 `e2a.team_event`
- `twinkle/gateway/message_handler.py` — 映射 `e2a.team_event` → 浏览器 `team.update`
- `twinkle/agentserver/server.py` — `main()` 装配 TeamLoop（若 `team.enabled`）

**不改**：`tools/base.py`、`hooks/`、`gateway/agent_client.py`、`gateway/web_channel.py`、前端初版（MVP 阶段只做最小 UI）。

---

## 6. 阶段 B（完整 Team）—— 暂不展开

阶段 A 跑通后，阶段 B 的工作清单：

- `TeamTaskStore`：任务队列 + 认领 + 依赖解除（可复用 `TodoStore` 的数据模型）
- `MemberMessageBus`：P2P 消息 + Broadcast（可在 `AgentLoop` 中注入 `before_model_call` hook 把消息 prepend 到 session）
- `TeamMonitorHandler`：从 member Event 收集 + 广播（独立 asyncio task 消费 `asyncio.Queue`）
- `TeamRecoveryManager`：member 崩溃自动重启（Phase 12 中断标记 + per-member `_sanitize_orphan_tool_calls`）
- 前端 Team 面板：成员列表 + 状态指示 + 实时活动流 + 中间产物预览
- Config 驱动的 member 模板注册表（取代硬编码的 `members` YAML 块）

---

## 7. 已知限制（阶段 A）

- **无成员间通信**：member 之间不能直接对话，只能靠 leader 转发。复杂协作场景（如 "研究员产出的 notes → 写作员引用"）靠共享 workspace 文件实现
- **无任务认领**：leader 显式分派给特定 member（`spawn_subagent(role="researcher")`），member 不会"主动认领"
- **无 Monitor 事件流**：前端只看到 leader 的文字流 + team_event 帧（member 完成/失败），看不到 member 内部的逐步活动
- **Leader 单点**：leader 崩溃则整个 team 停摆；无 leader 切换
- **无 team state 持久化**：跨重启 team 状态丢失（phase B 补）

---

## 8. 与 jiuwenswarm 的对照

| 维度 | jiuwenswarm | Twinkle 阶段 A | Twinkle 阶段 B |
|---|---|---|---|
| 多 Agent 生命周期 | `TeamAgent` SDK，8 态 lifecycle | `TeamLoop` 外层循环，member = AgentLoop | 成员状态机 + `TeamManager` |
| 任务分发 | Task Queue + claim/complete | Leader 显式分派（`spawn(role=...)`） | `TeamTaskStore` + claim/unblock |
| 成员通信 | P2P + Broadcast | 无（靠 leader 转发 + 共享文件） | `MemberMessageBus` |
| 监控事件 | 14 种 MonitorEvent | `e2a.team_event`（member 完成/失败） | `TeamMonitorHandler` + 全事件流 |
| 崩溃恢复 | `RecoveryManager` + SQLite | Phase 12 单 agent 恢复可用 | Team 级 member 自动重启 |
| 成员特化 | Rail 白名单 + Tool 白名单 + per-member skill | 配置驱动的 system_prompt + tools + skills | 成员模板注册表 |
| 共享状态 | SQLite shared DB | 共享 workspace 文件 | SQLite team state |

---

*本文与 Twinkle 代码库同步维护。实现前先 review 此分析，确认阶段划分和范围。*
