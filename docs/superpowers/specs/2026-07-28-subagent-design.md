# Twinkle Subagent 设计

> 日期：2026-07-28 · 状态：草案待 review
> 对齐：jiuwenswarm 子 agent 的**核心机制**（`task_tool`/`spawn_subagent` + `create_subagent` + 工具继承裁剪 + skill 按需 + 结果回灌）。v1 黑盒（子跑完回最终字符串），不展开流式。
> 重点：**如何创建 subagent、subagent 看到哪些上下文、如何用工具、能否用 skill、主 agent 如何委派并使用结果**。

---

## 0. 核心思想（从 jiuwenswarm 抽取 5 条）

1. **子 agent 就是一个工具**。父 agent 的正常 ReAct loop 发出 `tool_call: spawn_subagent(...)`，工具的 `invoke` 跑一个嵌套子 agent loop，把最终结果当 `tool_result` 返回。→ 复用现有工具调用面，`agent_loop` 零结构改动。（jiuwenswarm：`task_tool`/`spawn_subagent` 都是 `@tool`，父 `DeepAgent` 当普通工具调。）
2. **子 agent 用全新独立 session，只拿 objective 作 query，不继承父历史**。父负责在 `objective`/`prompt` 里给够上下文。（jiuwenswarm：`task_tool.invoke` 只传 `{"query": task_description, "conversation_id": sub_session_id}`，不传父消息。）
3. **子 agent 有自己的工具管理器 = 父工具减排除集**。排除集至少含子 agent 工具自身 → 递归保护；其余工具的 ReAct 调用机制照旧。（jiuwenswarm：`EXCLUDED_TOOLS_SPAWN` / `DISALLOWED_FOR_SUBAGENTS`。）
4. **子 agent 可用 skill，但按需加载、不自动塞清单**。（jiuwenswarm：`SubagentSkillUseRail` 跳过 skill 清单注入，仍允许 `skill_tool` 加载 body，`skill_complete` 时 force-finish。）
5. **结果回灌 + 停止提示**。子最终输出作 `tool_result`（+ 停止提示防父重复委派），回灌 `{role:"tool"}`，父下一轮 LLM 看到并整合。（jiuwenswarm：`_SUBAGENT_STOP_HINT`。）

下文逐条落到 Twinkle 实现。

---

## 1. 如何创建 subagent

### 入口
父 ReAct 中 LLM 输出 `tool_call: spawn_subagent(objective="…", role_id="MainAgent", prompt="…", model_name="")`。父 `agent_loop` 工具调用路径**原样**走（`agent_loop.py:213-273`）：
```
ctx.inputs = ToolCallInputs(name, args, tool_call_id)
→ BEFORE_TOOL_CALL（父 PermissionHook 若判 require-approval → 现有 e2a.ask 父 HITL，与普通工具一致）
→ result = await ToolManager.execute("spawn_subagent", args)   # 即 spawn_subagent.invoke(args)
→ result 回灌 {role:"tool",content:result}
```
`spawn_subagent` 是普通 `@tool`，`invoke` 返回字符串。父 loop 无任何特殊分支。

### `spawn_subagent` 工具
`twinkle/agentserver/tools/builtin/subagent_tools.py`：
```python
_SUBAGENT_STOP_HINT = (
    "\n\n[SYSTEM] The delegated task is complete. "
    "Summarize the result to the user and finish your turn. "
    "Do NOT call spawn_subagent again for this task."
)

@tool
async def spawn_subagent(objective: str, role_id: str = "MainAgent",
                         prompt: str = "", model_name: str = "") -> str:
    """Delegate an isolated subtask to a fresh sub-agent that runs its own ReAct
    loop in an isolated session and returns only its final answer.

    WHEN to delegate:
    - The subtask is complex / multi-step and benefits from focused ReAct.
    - You want it isolated (fresh context, can't pollute this conversation).
    - Different subtasks are independent (call spawn_subagent once each).

    WHEN NOT to delegate:
    - One tool call or a direct answer suffices — do it yourself.
    - The subtask needs this conversation's history — pass it explicitly in
      `objective` instead (the sub-agent CANNOT see this agent's history).

    `objective` must be self-contained: goal + constraints + all context the
    sub-agent needs (it sees nothing else). `prompt` may carry extra instructions
    (e.g. output format). The sub-agent cannot ask the user; it must converge or
    return a failure note. Its final answer (truncated if huge) becomes your
    tool_result — summarize it to the user; do not re-delegate the same task.
    """
    executor = get_subagent_executor()                 # ContextVar（见 §5）
    parent_sid = get_subagent_parent_session_id()      # ContextVar
    parent_rid = get_subagent_parent_request_id()      # ContextVar
    if executor is None or parent_sid is None:
        return "[subagent unavailable] executor not initialized"
    task = SubagentTaskSpec(objective=objective, role_id=role_id,
                            prompt=prompt, model_name=model_name)
    result = await executor.execute_spawn(task, parent_session_id=parent_sid,
                                          parent_request_id=parent_rid)
    return _wrap(result)   # success→result.result+STOP_HINT；failure→error+STOP_HINT
```
**为何无参取 executor/session**：`spawn_subagent` 注册时是单例（靠 ContextVar 找当前 executor/父 session），对齐 jiuwenswarm 的无参 `spawn_subagent` + ContextVar 桥。父 session_id 随请求变，必须 ContextVar（不能闭包）。

### `SubagentExecutor.execute_spawn` —— 创建并跑子 agent
`twinkle/agentserver/tools/subagent_executor/__init__.py`：
```python
class SubagentExecutor:
    def __init__(self, llm, store, parent_tools, config):
        self._llm = llm                    # 复用父 LLMClient（无状态，可跨 loop 共享）
        self._store = store                 # 复用父 SessionStore（同一进程单例）
        self._parent_tools = parent_tools   # 父 ToolManager，用于派生子工具集
        self._config = config
        self._active: dict[str, asyncio.Task] = {}   # task_id -> child task（取消用）

    async def execute_spawn(self, task, parent_session_id, parent_request_id):
        model = self._resolve_model(task.model_name)            # 见 §6
        child_sid = f"{parent_session_id}__sub_{uuid.uuid4().hex[:8]}"
        await self._store.create_session(child_sid)
        # 预注入子角色 system prompt（同时让 _inner_run_stream:141 的 TODO_SYSTEM_PROMPT seed 被跳过）
        await self._store.append(child_sid, {"role":"system","content": self._child_system_prompt(task)})

        child_loop = self._build_child_loop(model)              # 见下
        child_env = E2AEnvelope(
            request_id=f"{parent_request_id}__sub_{uuid.uuid4().hex[:8]}",
            session_id=child_sid, method="chat",
            params={"query": self._build_query(task)},          # objective（+prompt 拼接）
        )
        child_task = asyncio.create_task(self._drive_child(child_loop, child_env))
        self._active[task.task_id] = child_task
        try:
            final = await asyncio.wait_for(child_task, timeout=self._config.hard_timeout)
            return SubagentResult(success=True, task_id=task.task_id,
                                  role_id=task.role_id, result=final)
        except SoftTimeoutError as e:
            return SubagentResult(success=False, task_id=task.task_id, error=f"soft timeout: {e}")
        except asyncio.TimeoutError:
            return SubagentResult(success=False, task_id=task.task_id, error="hard timeout")
        except Exception as e:
            return SubagentResult(success=False, task_id=task.task_id,
                                  error=f"{type(e).__name__}: {e}")
        finally:
            self._active.pop(task.task_id, None)
```

### `_build_child_loop` —— 子 loop 构造
```python
def _build_child_loop(self, model):
    child_tm = self._build_child_tool_manager()               # 父工具减 EXCLUDED_TOOLS（见 §3）
    child_loop = AgentLoop(self._llm if model is None
                           else LLMClient(base_url=…, api_key=…, model=model),
                           self._store, child_tm,
                           max_steps=self._config.max_steps)   # 紧于父（默认 50 vs 1000）
    for h in (SkillHook(), MemoryHook(), LoggingHook()):       # 见 §4 / §3
        child_loop.register_hook(h)
    # 不注册 SubagentContextHook（子无 spawn_subagent，ContextVar 不会被读）
    # 不注册 PermissionHook（§3 子 HITL 规避）
    return child_loop
```
**复用 vs 新建**：`LLMClient`（无状态）+ `SessionStore`（单例）**复用**父的；`ToolManager`（要裁剪）**新建**；`AgentLoop` **新建**（自带 `HookManager`）。对齐 jiuwenswarm `create_subagent`：复用 model、新 session、裁剪 ability_manager、新 DeepAgent 实例、`max_iterations` 取紧值（机制 A 默认 15）。

### `_drive_child` —— 跑子 run（黑盒收集 final）
```python
async def _drive_child(self, child_loop, child_env) -> str:
    queue: asyncio.Queue = asyncio.Queue()
    async def _run():
        try:
            async for frame in child_loop.run_stream(child_env):
                await queue.put(frame)
        except Exception as e:
            await queue.put(e)
        finally:
            await queue.put(None)            # sentinel
    runner = asyncio.create_task(_run())    # context 副本 → 子 ContextVar.set 不回漏父
    final = ""
    while True:
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=self._config.soft_timeout)
        except asyncio.TimeoutError:
            runner.cancel(); raise SoftTimeoutError("no child activity")
        if frame is None: break
        if isinstance(frame, Exception): raise frame
        if frame.response_kind == "e2a.complete":
            final = frame.body.get("result", {}).get("content", "")
        elif frame.response_kind == "e2a.error":
            raise RuntimeError(frame.body.get("error", "child error"))
        # e2a.chunk / e2a.todo_update 丢弃（黑盒）
    await runner
    if len(final) > self._config.max_result_chars:      # 截断防父上下文爆炸
        final = final[:self._config.max_result_chars] + "\n…[truncated]"
    return final
```
- **`asyncio.create_task` 的作用**：复制 ContextVar context，子 `run_stream` 入口 `.set(PLAN_TODO_SESSION_ID=child_sid)`/`reset_todo_events()`/`set_permission_channel()` 不污染父（否则父 resume 后 todo 工具会写到子 session、父的待吐 todo 事件被清空）。
- **queue 的作用**：(a) 软超时（无活动）检测；(b) 黑盒只取 final、丢中间帧。无流式转发负担。
- **jiuwenswarm 对应**：`task_tool.invoke` 跑 `subagent.invoke(...)` 取 `result["output"]`；Twinkle 用 `run_stream` async gen + queue 收 `e2a.complete.content`。

---

## 2. subagent 应该看到哪些上下文

### 原则：全新 session，只看 objective，不继承父历史
子 session 消息列表最终形态（子 ReAct 跑开后）：
```
[
  {"role":"system", "content": <子角色 prompt>},        # 预注入，§1
  {"role":"user",   "content": <objective(+prompt 拼接)>},  # run_stream 入口追加，agent_loop:148-152
  {"role":"assistant", ...},                            # 子第一轮 LLM
  {"role":"tool", "tool_call_id":…, "content":…},       # 子工具结果
  ...                                                    # 子 ReAct 继续
]
```
**子看不到父 session 的任何消息**。对齐 jiuwenswarm spawn/task_tool（只传 query，不传父历史）。

### 为何不继承父历史
1. **隔离防污染**——父历史里可能有半成品 tool_call、用户闲聊、与子任务无关的内容，会干扰子 agent。
2. **控制上下文大小**——父历史可能很长，子塞进去既贵又分散注意力。
3. **对齐主线**——jiuwenswarm spawn/task_tool 皆不继承。
4. 若子**确实需要**父的上下文（如"基于刚才讨论的文档…"），**父把相关内容显式写进 `objective`/`prompt`**——这是父的责任，不是子的继承。

### 父该怎么写 objective（示例）
```
objective = """
分析以下 PR 的 diff，列出潜在风险点（只列，不改）：

<repo>: owner/repo
<branch>: feature/x
<diff>:
{{把父已拿到的 diff 内容贴这里，因为子看不到父历史}}
"""
```
关键：子是黑盒、看不到父对话，所以 objective 必须**自包含**（含目标 + 约束 + 必要背景）。`prompt` 可放补充指令（如输出格式）。

### 子角色 system prompt 模板
`SubagentExecutor._child_system_prompt(task)` 返回（v1 单一默认模板，`role_id` 仅作显示标签，无角色注册表）：
```
You are an isolated sub-agent. Your parent agent delegated a focused subtask to you.

Rules:
- You CANNOT see the parent's conversation history. Everything you need is in the user message (the objective). If something is missing, do your best with what you have — do NOT ask the user (you have no direct channel to them).
- You have the same tools as the parent EXCEPT spawn_subagent (you cannot delegate further).
- You may use skills: call list_skill to see available skills, read_skill(name, "SKILL.md") to load one.
- Work through the subtask with the ReAct loop. When done, return your final answer as the (plain) final message; that answer is returned to the parent.
- Be focused and concise — your output becomes the parent's tool_result.

Subtask role: {role_id}
"""
```
**为何强调"不要问用户"**：黑盒下子 agent 无 ws 通路（§3 子 HITL），若子停下来反问会卡死；必须让它尽力收敛或返回失败说明。

### jiuwenswarm 思想 → Twinkle 落地
- 思想：spawn/task_tool 只传 `{"query": task_description, "conversation_id": sub_session_id}`，子全新 session。
- 落地：子 `child_sid` 全新；预注入 system + `run_stream` 追加 objective 作 user query；父在 objective 里自包含上下文。

---

## 3. subagent 如何使用工具

### 子有独立 ToolManager（父工具减排除集）
`_build_child_tool_manager()`：
```python
EXCLUDED_TOOLS = {
    "spawn_subagent",                  # 递归保护：子不可再委派
    "write_memory", "edit_memory",      # 记忆只读：子不擅自写长期记忆（产出经返回串由父决定是否存）
}

def _build_child_tool_manager(self):
    child_tm = ToolManager()
    for t in self._parent_tools.list():
        if t.card.name not in EXCLUDED_TOOLS:
            child_tm.register(t)        # 复用同一 Tool 实例（工具靠 ContextVar/闭包自解析，实例可共享）
    return child_tm
```
子拿到的工具 = 父的全套（`web_fetch`/`web_search`/`command_exec`/`file_*`/`todo_*`/`list_skill`/`read_skill`/`memory_search`/`read_memory`）**减 `spawn_subagent` + `write_memory`/`edit_memory`**。即子记忆**只读**——能检索/读取长期记忆，但不能写入/编辑（对齐 jiuwenswarm 排除子的记忆回调工具；避免临时子 agent 悄悄改长期记忆）。

### 同一套 ReAct 工具调用机制
子 `run_stream`（即子 `AgentLoop`）跑的是**和父完全一样的闭环**（`agent_loop.py:155-308`）：
```
子每步: msgs = get_messages(child_sid)
        → compress_messages(…)
        → BEFORE_MODEL_CALL（子 SkillHook/MemoryHook 注入）
        → llm.stream(msgs, child_tm.schemas())
        → Finish(tool_calls) → 对每个 tc: ToolManager.execute(name,args) → 回灌 {role:"tool"} 到 child_sid
        → 下一步 get_messages(child_sid) 带上工具结果 → 再查询
        → Finish(stop) → yield e2a.complete(final_text)
```
**子调用工具的机制零特殊**——直接复用父的 `agent_loop`。`ToolManager.execute` 不抛异常（失败返 `"[tool error] …"` 字符串），故子工具失败不会崩子 loop，而是作为 tool_result 让子 LLM 自行处理。

**子 agent 自带上下文压缩 + 短期记忆**（复用 AgentLoop 内建，无需额外 rail）：
- **上下文压缩（无条件）**：每步 `compress_messages(msgs, …, token_threshold=CONTEXT_TOKEN_THRESHOLD, …)`（`agent_loop.py:159`），阈值用全局配置，和父同一套；子上下文超阈值即压缩。
- **短期记忆**：子自己的 `SessionStore` session（`child_sid`）即其短期记忆，`history.json` 持久化完整 ReAct 历史，每步 `get_messages(child_sid)` 重读（`agent_loop.py:156`）；子多步工具的中间结果都在此累积。
- **对比 jiuwenswarm**：短期记忆同样有（独立 `sub_session_id`）；但压缩是**条件性**的（仅当全局 `react.context_engine_config.enabled`，`executor.py:940/1124`，`minimal=True` 复用主 agent 配置）。Twinkle 无条件内建更简。

### 递归保护（为何只删 `spawn_subagent`、单层）
- `EXCLUDED_TOOLS` 含 `spawn_subagent` → 子 `child_tm.schemas()` 不含它 → 子 LLM 看不到 → 无法让子再委派 → **单层（深度 1）**。
- 为何用结构式（工具排除）而非深度计数器：(1) 简单、零运行时状态；(2) 对齐 jiuwenswarm（无运行时 depth 计数器，靠工具排除结构式封顶）；(3) 子 `ToolManager` 天然就是"能力边界"的体现。
- 为何只裁这两类：`spawn_subagent` 是递归保护；`write_memory`/`edit_memory` 是记忆只读（临时子 agent 不应擅自改长期记忆）。其余（`todo_*`/`memory_search`/`read_memory`/`list_skill`/`read_skill`/`file_*`/`command_exec`）对子有用，保留。后续要进一步收紧再加白名单。

### 子工具的 HITL：v1 不支持，靠不挂 PermissionHook 规避死锁
- 若子 loop 挂 `PermissionHook` 且子跑 `require-approval` 工具（如 `command_exec` 某档）→ 子 `run_stream` yield `e2a.ask` + `await future`。但黑盒不转发该帧 → `APPROVAL_REGISTRY` 无人 resolve → **死锁**。
- v1 规避：**子 loop 不挂 `PermissionHook`**（`child_permissions: false`）→ 子工具 tier 检查不发生 → 不触发子 ASK。
- **安全兜底**：`command_exec` 的 `builtin_rules` 黑名单（危险命令阻断）**不依赖 PermissionHook**（`command_exec` 内部直接读 `COMMAND_DENY_PATTERNS`），故子仍受基本安全约束。子跑文件工具也受 `TWINKLE_WORKSPACE_DIR` 沙箱约束。
- jiuwenswarm 思想：机制 C 子 agent 的审批通过 `SubagentSessionProxy` 流式透传 + `resolve_permission_approval` 路由到活跃子。Twinkle v1 无流式故不支持子 HITL；流式后置再开（复用 `APPROVAL_REGISTRY`，父循环不 suspend）。

### jiuwenswarm 思想 → Twinkle 落地
- 思想：`EXCLUDED_TOOLS_SPAWN`（机制 C）/ `DISALLOWED_FOR_SUBAGENTS`（机制 D）裁掉子 agent 工具自身 + 父级编排工具；子用同一 ReAct 机制。
- 落地：`EXCLUDED_TOOLS = {"spawn_subagent"}`（更宽，不裁编排工具）；子 loop 直接复用 `AgentLoop` 的工具调用闭环。

---

## 4. subagent 是否能使用 skill

### 能。子 loop 挂 `SkillHook()`，子 ToolManager 含 `list_skill`/`read_skill`
子 `build_child_loop` 注册 `SkillHook()`（无状态、安全——只读 `get_skill_manager()` 注入系统消息，不持 session 态）。`list_skill`/`read_skill` 未被 `EXCLUDED_TOOLS` 排除 → 子可用。

### 注入模式（沿用父 `SkillHook` 的 `skills.mode`）
`SkillHook.before_model_call` 按全局 `skills.mode` 注入到子 `ctx.inputs.messages`：
- `all`（默认）：每步注入 skill 清单（子看到可用 skill 列表 + 描述）。
- `auto_list`：注入一句"调 `list_skill` 查看可用 skill"，不塞全清单。

### 子用 skill 的完整流程
```
子 ReAct: LLM 看到 skill 清单 → 选一个 → tool_call: read_skill("doc-audit", "SKILL.md")
        → ToolManager.execute("read_skill", …) → 返回 SKILL.md body 作 tool_result
        → 回灌 {role:"tool"} 到 child_sid → 子下一轮 get_messages 带上 skill 指令
        → 子按 skill 的多步流程执行（用其它工具） → 收敛 → final
```
即子用 skill 和父用 skill **机制完全相同**（`skill_tools.py` 的 `list_skill`/`read_skill` + `SkillHook` 注入），只是发生在子的 ReAct 里。

### jiuwenswarm 的 `SubagentSkillUseRail` 思想 vs Twinkle 取舍
- jiuwenswarm：`SubagentSkillUseRail` 给子 agent **跳过 skill 清单注入**（保持子 prompt 精简，因为父级 skill 清单可能 ~62K），但仍允许 `skill_tool` 按需加载 body；且 `skill_complete` 时 `request_force_finish`（子完成 skill 直接结束，避免多余 stop 轮）。
- Twinkle 取舍：Twinkle 的 `SkillHook` 清单较轻（只有 `name`/`description`，非全 body），v1 **直接复用 `SkillHook()`**，模式随全局 `skills.mode`（建议子也用 `all` 或 `auto_list`，由配置统一控制）——**不另造子专用变体**，更简。Twinkle 无 `skill_complete` 工具，故 `skill_complete→force_finish` 不适用；子靠正常 `finish_reason=="stop"` 收敛（`agent_loop:279-288`）。
- 后续若要子 prompt 更精简（不塞清单、只提示按需加载），再造 `SubagentSkillHook`（强制 `auto_list`）变体——预留扩展点，v1 不做。

### jiuwenswarm 思想 → Twinkle 落地
- 思想：子可用 skill，但按需加载、清单注入可跳过。
- 落地：子复用 `SkillHook()` + `list_skill`/`read_skill` 不排除；模式随 `skills.mode`；v1 不做 skill 专用精简变体。

---

## 5. 主 agent 如何委派 subagent 并使用结果

### 完整数据流（黑盒）
```
父 ReAct: LLM → tool_call: spawn_subagent(objective=…)
  ├─ agent_loop: ctx.inputs=ToolCallInputs(name="spawn_subagent", args, tc_id)
  ├─ BEFORE_TOOL_CALL（父 PermissionHook：spawn_subagent 若 require-approval → e2a.ask 父 HITL，现有机制）
  ├─ result = await ToolManager.execute("spawn_subagent", args)
  │    └─ spawn_subagent.invoke(args):
  │         ├─ 取 ContextVar: executor / parent_sid / parent_rid   ← 由父 loop 的 SubagentContextHook.before_invoke 设
  │         ├─ task = SubagentTaskSpec(…)
  │         ├─ result = await executor.execute_spawn(task, parent_sid, parent_rid)
  │         │    ├─ create child_sid=<parent>__sub_<id>；store.create_session；预注入 system prompt
  │         │    ├─ build_child_loop（裁剪 tools + 注册 hooks + max_steps）
  │         │    ├─ child_task = asyncio.create_task(_drive_child(child_loop, child_env))  ← context 副本隔离
  │         │    ├─ asyncio.wait_for(child_task, hard_timeout)（软超时在 _drive_child 内 queue.get）
  │         │    └─ 返回 SubagentResult（success/result 或 error）
  │         └─ return result.result + _SUBAGENT_STOP_HINT   （或 error + STOP_HINT）
  ├─ 回灌: store.append(parent_sid, {role:"tool", tool_call_id:tc_id, content:result})   ← agent_loop:264-273
  ├─ flush_todo_events() → e2a.todo_update（父的，子 task 未污染）
  └─ 下一步: get_messages(parent_sid) 带上子结果 → 父 LLM 看到子输出 → 总结/继续 → e2a.complete 给用户
```
**关键**：父"使用子结果"靠的是**已有的工具结果回灌机制**（`agent_loop:264-273` 把 result 追加为 `{role:"tool"}`，下一轮 `get_messages` 带上）——零特殊"使用结果"代码。子结果在父看来就是一个 tool 的输出，和 `web_fetch` 的结果没本质区别。

### ContextVar 桥 + `SubagentContextHook`（仅父 loop）
`spawn_subagent` 无参取 executor/session，靠 ContextVar。`twinkle/agentserver/subagent_context.py`：
```python
SUBAGENT_EXECUTOR          = ContextVar("subagent_executor", default=None)
SUBAGENT_PARENT_SESSION_ID = ContextVar("subagent_parent_sid", default=None)
SUBAGENT_PARENT_REQUEST_ID= ContextVar("subagent_parent_rid", default=None)
def get_subagent_executor(): return SUBAGENT_EXECUTOR.get()
def get_subagent_parent_session_id(): return SUBAGENT_PARENT_SESSION_ID.get()
def get_subagent_parent_request_id(): return SUBAGENT_PARENT_REQUEST_ID.get()
```
`twinkle/agentserver/hooks/builtin/subagent_context_hook.py`：
```python
class SubagentContextHook(AgentHook):
    priority = 50
    def __init__(self, executor): self._executor = executor
    async def before_invoke(self, ctx):           # run_stream 入口点，每 run 一次
        SUBAGENT_EXECUTOR.set(self._executor)
        SUBAGENT_PARENT_SESSION_ID.set(ctx.session_id)
        SUBAGENT_PARENT_REQUEST_ID.set(ctx.request_id)
```
- **仅父 loop 注册**（`server.py:main`）：子 loop 无 `spawn_subagent` → 不需设这些 ContextVar → 不注册。
- **为何 `before_invoke` 而非 `before_tool_call`**：Twinkle 的 `run_stream` 入口已在设 ContextVar（`PLAN_TODO_SESSION_ID` 等，`agent_loop:135-137`），`before_invoke` 同是入口点；比 jiuwenswarm 的 `before_tool_call` 设 + `after_tool_call` 清更简（context 副本随 run 结束失效，无需 after 清理）。
- **为何不用闭包捕获 executor**：executor 是 per-loop 固定可闭包，但 `session_id`/`request_id` 随请求变，必须 ContextVar；为统一，executor 也走 ContextVar（对齐 jiuwenswarm 无参工具 + ContextVar 桥）。

### 停止提示 `_SUBAGENT_STOP_HINT`（防父重复委派）
每个子结果追加 `[SYSTEM] The delegated task is complete… Do NOT call spawn_subagent again for this task.`，让父 LLM 拿到结果后倾向"总结并结束"而非"再开一个子 agent 重做"。对齐 jiuwenswarm。

### 超时兜底（委派不能挂死父）
| 档 | 默认 | 机制 |
|---|---|---|
| 硬 | 300s | `asyncio.wait_for(child_task, hard)` 包整个子 run（`execute_spawn` 外层） |
| 软 | 120s | `_drive_child` 内 `asyncio.wait_for(queue.get(), soft)`；无活动 → `SoftTimeoutError` |
| abort | 30s | 父断连/超时取消子 task 时，给 `wait_for(child_task cancel, abort)` 退出窗口 |
超时/异常 → `SubagentResult(success=False, error=…)` + STOP_HINT 回灌，父据此决定下一步（重试/换方案/告知用户）。父断连 → `ws_handler` `finally`（`server.py:131-136`）取消父 `run_task` → `CancelledError` 传播进 `await execute_spawn` → 取消子 task。Twinkle 暂无显式 interrupt(supplement/cancel)，后置。

### jiuwenswarm 思想 → Twinkle 落地
- 思想：`task_tool.invoke` 跑子 `invoke(...)` 取 `result["output"]` 作 `ToolOutput`；`_SUBAGENT_STOP_HINT` 防 re-delegation；硬/软/abort 三档超时。
- 落地：`spawn_subagent.invoke` → `execute_spawn` → `asyncio.create_task(_drive_child)` + `wait_for`，取 `e2a.complete.content` 作 final；回灌靠现有 `{role:"tool"}` 机制；STOP_HINT 照搬；超时三档同构、默认值更紧。

---

## 6. 配置（`config.yaml` + `schema.py` + `__init__.py`）

```yaml
subagent:
  enabled: true              # 父 loop 注册 spawn_subagent + SubagentContextHook + 组装 SubagentExecutor
  max_steps: 50              # 子 ReAct 上限（紧于 agent.max_steps=1000）
  hard_timeout: 300         # 硬超时秒
  soft_timeout: 120         # 软超时（无活动）秒
  abort_timeout: 30          # 取消卡死子秒
  child_permissions: false    # 子 loop 挂 PermissionHook？v1 必须 false（true 需流式，未支持→启动拒）
  model: ""                  # ""=复用 llm.model；否则覆盖子模型
  max_result_chars: 8000      # 子最终结果截断上限（防父上下文爆炸）
  list_sessions_filter: true # session.list 隐藏 __sub_ 前缀
```
`SubagentConfig(_StrictModel, extra="forbid")` + `config/__init__.py` flatten 出 `SUBAGENT_*` 常量。`_resolve_model(model_name)`：`model_name` > `subagent.model` > 父 `llm.model`。

---

## 7. 文件清单与改动点

**新增**
- `twinkle/agentserver/tools/builtin/subagent_tools.py` — `spawn_subagent`（`@tool`，`invoke->str`）+ `_SUBAGENT_STOP_HINT` + `_wrap`
- `twinkle/agentserver/tools/subagent_executor/__init__.py` — `SubagentExecutor`（`execute_spawn`/`_drive_child`/`_build_child_loop`/`_build_child_tool_manager`/`_child_system_prompt`/`_build_query`/`_resolve_model`/`abort_active_subagents`）+ `create_subagent_executor` 工厂
- `twinkle/agentserver/tools/subagent_executor/models.py` — `SubagentTaskSpec`（`task_id`/`role_id`/`objective`/`prompt`/`model_name`）+ `SubagentResult`（`success`/`task_id`/`role_id`/`result`/`error`）+ `EXCLUDED_TOOLS`
- `twinkle/agentserver/hooks/builtin/subagent_context_hook.py` — `SubagentContextHook`
- `twinkle/agentserver/subagent_context.py` — 3 ContextVar + getter
- 测试：`tests/test_subagent_executor.py` / `tests/test_subagent_tools.py` / `tests/test_subagent_context.py`

**改动（最小）**
- `twinkle/agentserver/agent_loop.py` — `__init__` 加 `max_steps=None`（默认 `MAX_STEPS`）；`_inner_run_stream:155` `range(MAX_STEPS)`→`range(self._max_steps)`；`:317` 错误消息 `MAX_STEPS`→`self._max_steps`。**工具调用路径零改动**。
- `twinkle/agentserver/tools/__init__.py` — `tool_manager()` 保持不含 subagent（由 `build_agent_loop` 组装时注册）
- `twinkle/agentserver/server.py` — `build_agent_loop`/`main()`：建 `SubagentExecutor` + 注册 `spawn_subagent` 到 `tools` + 注册 `SubagentContextHook` 到 loop
- `twinkle/agentserver/sessions/store.py` — `list_sessions` 过滤 `__sub_` 前缀（按配置）
- `twinkle/config/schema.py` + `twinkle/resources/config.yaml` + `twinkle/config/__init__.py` — `SubagentConfig` + `subagent:` 块 + flatten

**不改**：`tools/base.py`、`e2a/models.py`、`gateway/message_handler.py`、`schema/message.py`、前端（黑盒无新帧）。

---

## 8. 验收（围绕核心机制）

1. **创建**：父调 `spawn_subagent` → 子 `AgentLoop` 用全新 `child_sid`、裁剪后的 `ToolManager`（无 `spawn_subagent`）、复用 `LLMClient`/`SessionStore`、`max_steps=50`、注册 `SkillHook`/`MemoryHook`/`LoggingHook`、不挂 `PermissionHook`/`SubagentContextHook`。
2. **上下文**：`get_messages(child_sid)` 不含父历史，只有 `[system(子角色), user(objective), …子 ReAct]`；父在 objective 里自包含所需上下文。
3. **工具**：子用同一 ReAct 闭环调工具（`web_fetch`/`command_exec`/…）；子 `schemas()` 不含 `spawn_subagent`（单层）也不含 `write_memory`/`edit_memory`（记忆只读）；子工具失败回灌 `[tool error]` 不崩子 loop；`command_exec` 黑名单仍生效。
4. **skill**：子可 `list_skill`/`read_skill` 加载 skill body 并按其执行；`SkillHook` 按 `skills.mode` 注入。
5. **委派与结果**：父 `tool_call` → `spawn_subagent.invoke` → 子跑完 → `result.result + STOP_HINT` 回灌 `{role:"tool"}` → 父下一轮看到并总结给用户；`STOP_HINT` 抑制父重复委派；子结果超 `max_result_chars` 截断防父上下文爆炸。
6. **隔离**：子 run 在 `asyncio.create_task`（context 副本），父的 `PLAN_TODO_SESSION_ID`/`TODO_EVENTS` 不被污染。
7. **超时**：硬/软/abort 三档，超时/异常返回 `success=False` + STOP_HINT、不挂死父。
8. **session 列表**：`session.list` 不显示 `__sub_` 子会话。
9. 测试全绿（不引入 `pytest-asyncio`）。

---

## 9. 已知限制与后置

**v1 已知限制（写明，非缺陷）**
- **并行 subagent 实为串行**：父 `agent_loop` 的 `for tc in tcs:`（`agent_loop.py:213-273`）顺序处理每个 tool_call。即使 LLM 一轮 emit 多个 `spawn_subagent`，也是一个跑完再跑下一个，不并发。并发需改 agent_loop 工具调用循环为 `asyncio.gather`（影响所有工具，非仅 subagent）——列为后置通用改进。当前并发靠主 agent 多次串行调用。
- **子 agent 工作区与父共享**：子 `file_*`/`command_exec` 受 `TWINKLE_WORKSPACE_DIR` 约束但与父共享同一目录，可能覆盖父正在写的文件（沙箱兜底不能逃逸 workspace，低风险）。独立子目录 `<workspace>/sub_agents/<child_sid>` + 子 `output_files` 回传父，列为后置。
- **子 session 磁盘累积**：`SessionStore` 磁盘持久，子 session（`__sub_<id>`）跑完不删、随时间堆积（仅从 `list_sessions` 隐藏）。v1 保留以便事后 debug 失败子 agent（看其 `history.json`）；父 `session.delete` 级联删子的清理列为后置。
- **子 token 不计入父 usage**：黑盒不回传子 usage；子 span 在 OTel/LoggingHook 可单独看，v1 不聚合到父。
- **disabled 行为**：`subagent.enabled: false` 时 `spawn_subagent` 不注册 → LLM 调用得 "unknown tool"；v1 接受此降级（后续可改为始终注册但返 "[subagent disabled]" 明示）。

**后置（依赖流式或更大改动）**
- 子 HITL（需流式转发 `e2a.subagent_ask`）、流式转发（model delta 透传 + 并行解复用）、多级嵌套（当前单层）、team/swarm 多 agent 编排、interrupt(supplement/cancel) 显式中断、skill 声明角色（roles/allowed_tools/parallel_max）、model_tier 分档。
