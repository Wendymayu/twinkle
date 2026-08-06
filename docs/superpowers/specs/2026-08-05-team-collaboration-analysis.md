# Twinkle Agent Team 协作 · 阶段 A 架构设计

> 日期：2026-08-05 · 状态：设计完成，待实现
> 对齐：jiuwenswarm `TeamManager` + `TeamAgent`

---

## 1. 目标

1 leader + 2-3 角色化 member 并发执行子任务，leader 整合结果输出最终答案。

**不做**：成员间直接通信、任务队列、Monitor 事件流、前端成员面板、team 级崩溃恢复。

---

## 2. 对象模型

对齐 jiuwenswarm 的两层结构：

```
jiuwenswarm                          Twinkle Phase A
─────────────────────────────        ─────────────────────────
TeamManager (全局单例)               TeamManager (全局单例)
  _team_agents: dict[sid, TeamAgent]   _teams: dict[sid, Team]
  create_team(sid) → TeamAgent         create_team(sid) → Team
  get_or_create_team(sid)              get_or_create_team(sid)

TeamAgent (openjiuwen SDK)           Team (per session)
  leader + members (DeepAgent[])       _members: dict[key, ReActAgent]
  task_queue                           workspace: Path
  event_bus                            delegate(persona, objective) → str
  shared_state (SQLite)                cleanup()
```

**职责边界**：
- `TeamManager`：全局注册表。创建、获取、销毁 Team 实例。不直接操作 member。
- `Team`：一个 session 内的团队。管理 member agent 生命周期、处理委派、维护共享 workspace。
- `delegate_to_member` 工具：薄包装。从 ContextVar 拿 Team → 调 `team.delegate()`。

---

## 3. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  config.yaml                                                 │
│  team:                                                       │
│    enabled: true                                             │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│  server.py create_agent()                                    │
│                                                              │
│  team_mgr = TeamManager(llm, store, parent_tools, config)    │
│  hooks += [TeamContextHook(team_mgr)]                        │
└────────┬────────────────────────────────────────────────────┘
         │
         │  TeamContextHook.before_invoke:
         │    team = team_mgr.get_or_create_team(session_id)
         │    CURRENT_TEAM.set(team)
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  TeamManager (全局)                                          │
│  _teams: dict[session_id, Team]                              │
│  get_or_create_team(sid) → Team                              │
│  destroy_team(sid)                                           │
└────────┬─────────────────────────────────────────────────────┘
         │  creates / retrieves
         ▼
┌──────────────────────────────────────────────────────────────┐
│  Team (per session)                                          │
│                                                              │
│  _members: dict[key, ReActAgent]   ← 缓存，跨 delegation 复用 │
│  workspace: Path                   ← team/<sid>/shared/      │
│                                                              │
│  delegate(persona, objective) → str                          │
│    ├─ 按 MEMBER_TOOL_WHITELIST 构建 ToolManager              │
│    ├─ system_prompt = base + persona + workspace 提示         │
│    ├─ 创建/复用 member ReActAgent                            │
│    ├─ agent.run() → 收敛                                     │
│    └─ 返回 final content                                    │
└────────┬─────────────────────────────────────────────────────┘
         │  ContextVar → delegate_to_member 工具
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│              ReActAgent._run_react_loop  (UNCHANGED)          │
│                                                              │
│  Step N: LLM → 并发 tool_calls:                              │
│    delegate_to_member(persona="金融分析师", objective="...")   │
│    delegate_to_member(persona="报告撰写人", objective="...")   │
│          ↓                                                   │
│          _try_parallel_tool_calls → asyncio.gather           │
│          ↓                      ↓                            │
│     ┌──────────────┐     ┌──────────────┐                   │
│     │ 金融分析师   │     │ 报告撰写人   │                   │
│     │ ReActAgent   │     │ ReActAgent   │                   │
│     │ 白名单工具   │     │ 白名单工具   │                   │
│     │ ws: shared/  │     │ ws: shared/  │                   │
│     └──────┬───────┘     └──────┬───────┘                   │
│            └────────┬──────────┘                             │
│                     ↓                                        │
│  Step N+1: LLM 整合结果 → 继续委派 or 输出                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 组件设计

### 4.1 常量（team/manager.py）

```python
# 对齐 jiuwenswarm team_runtime_inheritance.py TOOL_WHITELIST
MEMBER_TOOL_WHITELIST: frozenset[str] = frozenset({
    "web_search", "web_fetch",
    "read_file", "write_file", "edit_file", "list_files", "glob",
    "command_exec",
    "memory_search", "read_memory",
    "todo_create", "todo_update", "todo_list", "todo_get",
    "list_skill", "read_skill",
    "cron_list_jobs", "cron_create_job", "cron_update_job",
    "cron_delete_job", "cron_run_now",
})
```

### 4.2 TeamManager（team/manager.py）

```python
class TeamManager:
    """全局单例。session → Team 注册表。

    对齐 jiuwenswarm TeamManager._team_agents: dict[session_id, TeamAgent]。
    """

    def __init__(self, llm: LLMClient, store: SessionStore,
                 parent_tools: ToolManager, config: TeamConfig):
        self._llm = llm
        self._store = store
        self._parent_tools = parent_tools
        self._config = config
        self._teams: dict[str, Team] = {}

    def get_or_create_team(self, session_id: str) -> Team:
        """获取或创建 session 的 Team 实例。"""
        if session_id not in self._teams:
            self._teams[session_id] = Team(
                llm=self._llm,
                store=self._store,
                parent_tools=self._parent_tools,
                session_id=session_id,
                config=self._config,
            )
        return self._teams[session_id]

    def destroy_team(self, session_id: str) -> None:
        if team := self._teams.pop(session_id, None):
            team.cleanup()
```

### 4.3 Team（team/manager.py）

```python
class Team:
    """一个 session 内的团队实例。管理 member agent 生命周期。

    对齐 jiuwenswarm TeamAgent：持有 members、处理委派、维护 workspace。
    """

    def __init__(self, llm, store, parent_tools, session_id, config):
        self._llm = llm
        self._store = store
        self._parent_tools = parent_tools
        self._session_id = session_id
        self._config = config
        self._members: dict[str, ReActAgent] = {}
        self.workspace = team_workspace_dir(session_id)
        ensure_team_workspace(session_id)

    def _member_key(self, persona: str) -> str:
        import hashlib
        return hashlib.blake2b(persona.encode(), digest_size=8).hexdigest()

    def _get_or_create_member(self, persona: str) -> ReActAgent:
        key = self._member_key(persona)
        if key in self._members:
            return self._members[key]

        member = self._build_member(persona)
        self._members[key] = member
        return member

    def _build_member(self, persona: str) -> ReActAgent:
        # 1. ToolManager → 按 MEMBER_TOOL_WHITELIST 过滤
        tm = ToolManager()
        for t in self._parent_tools.list():
            if t.card.name in MEMBER_TOOL_WHITELIST:
                tm.register(t)

        # 2. 创建 session，写入 persona
        member_sid = f"{self._session_id}__team_{self._member_key(persona)}"
        self._store.create_session(member_sid)
        sp = build_system_prompt() + "\n\n" + persona
        sp += f"\n\n你的工作目录是 `{self.workspace}`。"
        self._store.append(member_sid, {"role": "system", "content": sp})

        # 3. 构建 ReActAgent
        hooks = [SkillHook(), MemoryHook(), LoggingHook(), RetryHook()]
        return ReActAgent(self._llm, self._store, tm,
                          hooks=tuple(hooks),
                          max_steps=SUBAGENT_MAX_STEPS)

    async def delegate(self, persona: str, objective: str,
                       prompt: str = "") -> str:
        """委派任务给 member，运行到收敛，返回最终结果。"""
        member = self._get_or_create_member(persona)
        member_sid = f"{self._session_id}__team_{self._member_key(persona)}"
        query = f"{objective}\n\n{prompt}" if prompt else objective
        request = AgentRequest(
            session_id=member_sid,
            request_id=f"{self._session_id}__team_{uuid4().hex[:8]}",
            query=query)
        return await self._drive_member(member, request)

    async def _drive_member(self, member, request) -> str:
        """运行 member agent 到收敛，返回 final content。"""
        # …（和 SubagentExecutor._drive_child 同款逻辑）
        # soft_timeout → "[member timeout]"
        # exception → "[member error]"
        # e2a.complete → final content
        # 截断到 SUBAGENT_MAX_RESULT_CHARS

    def cleanup(self):
        self._members.clear()
```

### 4.4 delegate_to_member 工具（tools/builtin/team_tools.py）

```python
@tool
async def delegate_to_member(persona: str, objective: str,
                             prompt: str = "") -> str:
    """委派任务给团队的一个成员。

    persona: 成员角色描述。如 "金融分析师，专长美股财报分析"
    objective: 任务目标（自包含，成员看不到这段对话）
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    return await team.delegate(persona, objective, prompt)
```

### 4.5 TeamContextHook

```python
# team/context.py
CURRENT_TEAM: ContextVar[Team | None] = ContextVar("team", default=None)

# hooks/builtin/team_context_hook.py  priority = 45
class TeamContextHook(AgentHook):
    def __init__(self, manager: TeamManager):
        self._manager = manager

    async def before_invoke(self, ctx: HookContext):
        team = self._manager.get_or_create_team(ctx.session_id)
        CURRENT_TEAM.set(team)
```

### 4.6 其余

- `TeamConfig`：只有 `enabled: bool = False`
- `TeamPrompt`：`build_system_prompt()` 条件追加委派指南
- `TeamWorkspace`：`team/workspace.py` — `team_workspace_dir` + `ensure_team_workspace`

---

## 5. 数据流

```
用户: "分析 AAPL Q3 财报"
  │
  ▼ ReActAgent.run()
  │  TeamContextHook → team_mgr.get_or_create_team(sid) → Team()
  │    CURRENT_TEAM.set(team)
  │    build_system_prompt() 注入委派指南
  │
  ▼ LLM → 并发 tool_calls:
      delegate_to_member(persona="金融分析师，专长美股财报",
                         objective="分析 AAPL 10-Q")
      delegate_to_member(persona="金融报告撰写人",
                         objective="写成结构化分析报告")
  │
  ▼ _try_parallel_tool_calls → asyncio.gather
  │  ├── Team.delegate("金融分析师", "分析 AAPL 10-Q")
  │  │     → _get_or_create_member: 首次，构建 ReActAgent
  │  │     → member.run() → 搜集数据 → 返回分析摘要
  │  └── Team.delegate("金融报告撰写人", "写成报告")
  │        → 构建另一个 ReActAgent → 返回报告
  │
  ▼ tool_results → leader session → LLM 整合 → 输出最终答案
```

---

## 6. 设计决策

| 决策 | 理由 |
|---|---|
| TeamManager + Team 两层 | 对齐 jiuwenswarm TeamManager + TeamAgent。TeamManager 是注册表，Team 管 member 生命周期 |
| Team 是 per-session 对象 | 对应 jiuwenswarm 的 `_team_agents: dict[session_id, TeamAgent]` |
| Team 负责 delegate | member 创建/复用/运行都在 Team 内，不外泄给工具函数 |
| 工具只从 ContextVar 拿 Team | delegate_to_member 是薄包装，逻辑全在 Team |
| 硬编码 MEMBER_TOOL_WHITELIST | 对齐 jiuwenswarm TOOL_WHITELIST |
| 所有 member 同一工具集 | 对齐 jiuwenswarm。差异仅靠 persona |
| 不改 ReActAgent | step 循环 + 并行执行已存在 |

---

## 7. 文件变更

| 操作 | 文件 | ~行数 |
|---|---|---|
| 新增 | `team/__init__.py` | 5 |
| 新增 | `team/context.py` | 6 |
| 新增 | `team/workspace.py` | 25 |
| 新增 | `team/manager.py` — TeamManager + Team + MEMBER_TOOL_WHITELIST | 150 |
| 新增 | `tools/builtin/team_tools.py` — delegate_to_member | 18 |
| 新增 | `hooks/builtin/team_context_hook.py` | 20 |
| 改动 | `config/schema.py` — TeamConfig | 10 |
| 改动 | `config/__init__.py` — TEAM_ENABLED | 4 |
| 改动 | `config.yaml` — `team: {enabled: false}` | 4 |
| 改动 | `tools/__init__.py` — 注册 delegate_to_member | 2 |
| 改动 | `agent.py` — build_system_prompt() 追加 team 段 | 20 |
| 改动 | `server.py` — create_agent() 创建 TeamManager + hook | 12 |
| 改动 | `hooks/builtin/__init__.py` — 导出 TeamContextHook | 2 |

**总计 ~280 行**。

## 8. 验证

1. 金融：2 个不同 persona 的 member 并发执行 → leader 整合输出分析报告
2. 同 persona 第二次委派 → 复用 member 实例和 session 历史
3. `team.enabled: false` → 无注入、无 delegate_to_member 工具
4. member 调 delegate_to_member → `[error] unknown tool`

## 9. 已知限制

- leader 靠 LLM 自决策何时委派
- 无成员间直接通信（靠共享 workspace）
- 无 Monitor 事件流
- leader 单点故障
- 无 team state 持久化
