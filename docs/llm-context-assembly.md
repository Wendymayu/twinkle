# LLM 上下文组装详解

> 本文追踪 Twinkle 从收到一条用户消息到最终把 `messages` + `tools` 交给 OpenAI API 的完整管线——每一步在哪个文件、加什么内容、为什么这样设计。

---

## 1. 全局管线总览

一次 LLM 调用的上下文组装分 **六个阶段**，按固定顺序执行：

```
请求到达 ──→ ① 会话初始化 ──→ ② 消息拉取 ──→ ③ 上下文压缩 ──→ ④ Hook 注入 ──→ ⑤ force_finish 检查 ──→ ⑥ LLM 调用
```

每个 ReAct 步骤都会完整走一遍 ②→⑥；① 只在请求入口执行一次。

| 阶段 | 代码位置 | 作用 |
|------|----------|------|
| ① 会话初始化 | `agent_loop.py:135-152` | ContextVar 绑定、orphan 修补、TODO system prompt 注入、用户 query 入库 |
| ② 消息拉取 | `agent_loop.py:156` → `sessions/store.py:181-189` | 从 SessionStore 取出 OpenAI-native 消息列表 |
| ③ 上下文压缩 | `agent_loop.py:159-165` → `context_compression.py:89-114` | 超阈值时把中间段摘要为一条 system 消息 |
| ④ Hook 注入 | `agent_loop.py:168-169` → `hooks/manager.py:80-98` | SkillHook(priority=90)、MemoryHook(priority=80) 各 prepend 一条 system 消息 |
| ⑤ force_finish 检查 | `agent_loop.py:172-182` | 若 Hook 发出拦截信号，跳过 LLM 调用直接返回 |
| ⑥ LLM 调用 | `agent_loop.py:193` → `llm_client.py:42-120` | `ctx.inputs.messages` + `ctx.inputs.tools` 交给 OpenAI SDK |

下面逐阶段展开。

---

## 2. ① 会话初始化（请求入口，执行一次）

### 2.1 ContextVar 绑定

```python
# agent_loop.py:135-137
PLAN_TODO_SESSION_ID.set(session_id or "default")   # Todo 工具通过它定位当前会话
reset_todo_events()                                  # 清空本请求的 todo 事件缓冲
set_permission_channel(envelope.channel or "web")    # PermissionEngine 通过它识别通道
```

三个 ContextVar 让无参数的工具函数（`todo_create`、`command_exec` 等）能自动获取请求级上下文，不需要把 `session_id` / `channel` 逐层传递。

### 2.2 Orphan 修补

```python
# agent_loop.py:138
await self._sanitize_orphan_tool_calls(session_id, envelope.request_id)
```

如果上一次请求中途崩溃（比如人机审批未完成），最后的 assistant 消息可能带 `tool_calls` 但缺少对应的 `tool` 角色结果——这会违反 OpenAI 消息合约。`_sanitize_orphan_tool_calls` 扫描历史，为每个缺失的 `tool_call_id` 补一条 `[interrupted: previous request did not complete]` 合成结果。

### 2.3 TODO system prompt 注入

```python
# agent_loop.py:210-216
messages = self._session_store.get_messages(session_id)
if not messages or messages[0].get("role") != "system":
    await self._session_store.append(session_id, {"role": "system", "content": build_system_prompt()}, ...)
```

`build_system_prompt()` 动态构建基础系统提示词，包含五个维度：

1. **身份与行为原则** — 对外不提内部细节；直接进入正题、先想再做、办事严谨、尽量不拒绝、简洁输出
2. **运行环境** — 当前平台（`sys.platform`）、当前日期、跨平台命令语法差异对照表
3. **工作区概览** — workspace/memory/skills 目录路径表
4. **Todo 工具用法** — todo_create/todo_complete/todo_list 的使用时机
5. **长期记忆与技能** — 简要提及工具存在，详细规则由 MemoryHook/SkillHook 注入

平台、日期、目录路径在注入时动态拼入（`build_system_prompt()` 是函数而非常量），确保会话首条 system 消息反映当时的运行状态。

**只在每个会话的第一条消息时注入**——检测到 `messages[0]` 已经是 `system` 角色后不再重复。这条消息写入 `history.json`，成为会话历史的第一条，**永不被压缩裁掉**（压缩总是保留 `head = msgs[0]`）。

### 2.4 用户 query 入库

```python
# agent_loop.py:147-152
query = (envelope.params or {}).get("query", "")
await self._session_store.append(session_id, {"role": "user", "content": query}, ...)
```

用户输入直接 append 到 `history.json`，成为消息列表的最新一条。

---

## 3. ② 消息拉取（每步执行）

```python
# agent_loop.py:156
msgs = self._session_store.get_messages(session_id)
```

`SessionStore.get_messages()` (`sessions/store.py:181-189`) 的行为：

- **缓存命中**：直接返回 `list(cached)`（浅拷贝，避免 AgentLoop 修改污染 store 内部列表）
- **缓存未命中**：从 `history.json` 逐条冷恢复，每条记录经过 `_record_to_openai()` 过滤，只保留四个 OpenAI 字段：`role`、`content`、`tool_calls`、`tool_call_id`。元数据字段（`id`、`request_id`、`channel_id`、`timestamp` 等）不会出现在 LLM 看到的消息中。

此时的消息结构（假设会话有过一轮对话）：

```
[0] system   — SYSTEM_PROMPT（build_system_prompt() 动态构建）
[1] user     — 上一轮用户输入
[2] assistant — 上一轮模型回复（或 tool_calls）
[3] tool     — 上一轮工具结果（如有）
[4] user     — 当前请求的 query
```

---

## 4. ③ 上下文压缩（每步执行）

```python
# agent_loop.py:159-165
msgs = await compress_messages(
    msgs, self._llm,
    token_threshold=CONTEXT_TOKEN_THRESHOLD,      # 默认 60000
    keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS,   # 默认 6
    summary_system_prompt=CONTEXT_SUMMARY_PROMPT,
)
```

`compress_messages()` (`context_compression.py:89-114`) 的完整流程：

### 4.1 Token 估算

```python
estimate_tokens(msgs)  # char // 3，CN/EN 折中估计
```

遍历所有消息，累加 `content` 字符长度和 `tool_calls` 的 `function.name` + `function.arguments` 长度，最后除以 3。不依赖 tiktoken，性能开销极小。

### 4.2 阈值判断

```python
if estimate_tokens(msgs) <= token_threshold:  # 默认 60000
    return list(msgs)  # 不压缩，浅拷贝返回
```

未超阈值 → 直接返回浅拷贝，**压缩零开销**。

### 4.3 三段分割

超阈值后，`_split_keep_tool_pairs()` 把消息分成 head / middle / tail：

| 段 | 内容 | 处理 |
|----|------|------|
| **head** | `msgs[0]`（第一条 system 消息，即 SYSTEM_PROMPT） | **完整保留**，永不压缩 |
| **tail** | 末尾 `keep_recent_pairs * 2 = 12` 条消息 | **完整保留**。如果 tail 起始位置落在 `tool` 角色，向左扩展到包含对应的 `assistant(tool_calls)`，保证 OpenAI 消息合约不被破坏 |
| **middle** | head 和 tail 之间的所有消息 | **LLM 摘要压缩** |

### 4.4 LLM 摘要

```python
# context_compression.py:75-86
async def _summarize(llm, summary_system_prompt, middle_text):
    messages = [
        {"role": "system", "content": summary_system_prompt},
        {"role": "user", "content": "把以下历史对话压成摘要，保留关键事实与工具结果：\n\n" + middle_text},
    ]
    # tools=[] — 纯文本生成，不让摘要模型调工具
    async for ev in llm.stream(messages=messages, tools=[]):
        ...
```

用**同一个 LLMClient**（同模型、同 API key）单独做一次摘要调用。摘要模型看到的是 `summary_system_prompt` + middle 段的纯文本渲染，**不携带任何工具 schema**。

### 4.5 结果组装

```python
# context_compression.py:113-114
summary_msg = {"role": "system", "content": f"[prior context summary] {summary}"}
return head + [summary_msg] + tail
```

压缩后的消息结构：

```
[0] system   — SYSTEM_PROMPT（head，原样保留）
[1] system   — "[prior context summary] <摘要文本>"（middle 的压缩替代）
[2..] user/assistant/tool — 最近 12 条消息（tail，原样保留）
```

### 4.6 降级策略

如果摘要 LLM 调用失败（网络异常、模型拒绝等），**不崩溃**，而是直接丢弃 middle 段：

```python
# context_compression.py:108-112
return head + tail  # 摘要是优化不是关键路径——降级为无摘要滑动窗口
```

### 4.7 关键设计：无损历史、有损视图

压缩结果 **不回写 SessionStore**。`history.json` 始终保存完整对话。压缩只塑造 LLM 的"即时视角"，每个 ReAct 步骤重新从 store 拉取、重新压缩。这意味着：

- 压缩不会丢失信息（完整历史始终在磁盘上）
- 摘要质量随模型能力自然提升，无需手动维护
- 同一会话的不同步骤可能产出不同的摘要（因为 tail 窗口滑动）

---

## 5. ④ Hook 注入（每步执行）

```python
# agent_loop.py:168-169
ctx.inputs = ModelCallInputs(messages=msgs, tools=self._tool_manager.schemas())
await self._hook_manager.execute(HookEvent.BEFORE_MODEL_CALL, ctx)
```

`ModelCallInputs` 包含两个字段：
- `messages: list[dict]` — 压缩后的消息列表
- `tools: list[dict]` — 17 个工具的 OpenAI function-calling schema

`HookManager.execute()` 按 **priority 降序**（数字越大先执行）依次调用所有 `before_model_call` 回调。当前注册的三个 Hook：

### 5.1 SkillHook（priority=90，最先执行）

**文件**：`hooks/builtin/skill_hook.py`

**条件**：`get_skill_manager().list_skills()` 返回非空列表时生效；无 skill → no-op。

**行为**：在 `ctx.inputs.messages` 头部 prepend 一条 system 消息：

- `"all"` 模式（默认）：
  ```
  ## 可用技能
  1. skill_name_1: skill_description_1
  2. skill_name_2: skill_description_2
  ...
  ```
- `"auto_list"` 模式：
  ```
  你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。
  ```

**注入方式**：`ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages`（赋新 list，不原地 insert）。

### 5.2 MemoryHook（priority=80，第二个执行）

**文件**：`hooks/builtin/memory_hook.py`

**条件**：`get_memory_manager().list_files()` 返回非空时生效；空 store → no-op。

**触发时机**：`before_invoke`（每个 ReAct 步骤入口；早于 `before_model_call`）。

**行为**：往 `ctx.extra["frozen_sections"]` stash 两个 `PromptSection`（loop 每步套用到 `SystemPromptBuilder`，按 priority 升序 join 进首条 system 消息）：

- `memory_strategy`（priority 80，常开）：使用策略 prompt——何时搜/写、三类文件语义、daily 不自动注入需 `memory_search('daily_memory/<日期>')`：

```
## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/{today}.md", ...)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。
```

其中 `{mem_dir}` 来自 `config.MEMORY_DIR`。

- `memory_static`（priority 81，opt-in，`memory.auto_inject.enabled` 默认开）：被动召回 `USER.md` + `MEMORY.md` 全文注入。两者**各走自己的字符预算**（`max_chars_user` / `max_chars_memory`，默认 4000 / 12000，对齐 openclaw 分文件预算）；超限**各自 head+tail 截断**（保首尾丢中间，对齐 openclaw `trimBootstrapContent`——首部=画像/核心偏好稳定，尾部=最近事实，丢中间陈旧段）。`daily_memory` **不**自动注入——需 daily 时模型 `memory_search('daily_memory/<日期>')`（= tool message = 动态区）。空 store → 两 section 都不 stash。

### 5.3 LoggingHook（priority=10，最后执行）

**文件**：`hooks/builtin/logging_hook.py`

**行为**：**不修改消息**。仅输出 `logging.info()` 日志（"LLM call starting, session=..."），纯可观测性用途。

### 5.4 注入顺序与合并

Hook 按优先级降序执行：SkillHook(90) → MemoryHook(80) → LoggingHook(10)。

每个 Hook 的注入方式是 **prepend**（把自己加到 list 最前面），所以 Hook 执行后多条 system 消息会按 prepend 反序排列。但 LLM 对中间上下文容易丢失注意力（"lost in the middle"问题），身份原则不应被埋在中间。

因此在 Hook 执行后，`_merge_system_messages()` 会把所有头部连续的 system 消息合并为一条，并按以下顺序排列内容（参考 jiuwenswarm 的 SystemPromptBuilder，低 priority = 靠前 = 利用 LLM 开头注意力热点）：

```
1. SYSTEM_PROMPT（身份与行为原则 + 运行环境 + 工作区 + 工具指南）  ← 开头注意力最强
2. 技能清单 / "调 list_skill" 提示                                    ← 身份之后
3. 长期记忆使用策略                                                   ← 技能之后
4. 压缩摘要 "[prior context summary]"（如有）                        ← 靠近对话，利用 recency bias
5. 其他未知 system 段                                                 ← 末尾
```

合并后发给 LLM 的消息结构变为：

```
[0] system — 合并后的唯一 system 消息（身份 → 技能 → 记忆 → 压缩摘要）
[1+] user/assistant/tool — 对话历史（tail 区域，完整保留）
[N] user — 当前请求的 query
```

**只有一条 system 消息**——对齐 jiuwenswarm 的 `SystemPromptBuilder.build()`，确保身份原则占据开头注意力热点，操作指南靠近对话利用 recency bias。

---

## 6. ⑤ force_finish 检查

```python
# agent_loop.py:172-182
ff = ctx.consume_force_finish_request()
if ff is not None:
    yield E2AResponse(..., response_kind="e2a.complete", body={"result": {"content": str(ff.result)}})
    return
```

如果某个 Hook 在 `before_model_call` 中调了 `ctx.request_force_finish(reason)`（比如安全拦截），LLM 调用被跳过，直接返回拦截消息。当前实现中，只有 `PermissionHook` 在 `before_tool_call` 事件中使用此信号，`before_model_call` 阶段无 Hook 会触发 force_finish。

---

## 7. ⑥ LLM 调用

```python
# agent_loop.py:193
async for ev in self._llm.stream(messages=ctx.inputs.messages, tools=ctx.inputs.tools):
```

**注意**：使用的是 `ctx.inputs.messages`（Hook 可能修改过），而非步骤 ② 的局部变量 `msgs`。

`LLMClient.stream()` (`llm_client.py:42-120`) 构建 OpenAI API 请求：

```python
kwargs = {
    "model": self._model,               # 默认 gpt-4o-mini
    "messages": messages,                # ctx.inputs.messages
    "stream": True,
    "stream_options": {"include_usage": True},
}
if tools:
    kwargs["tools"] = tools              # 17 个工具 schema
```

`tools` 参数使用 OpenAI function-calling 格式，由 `ToolManager.schemas()` 生成：

```python
{
    "type": "function",
    "function": {
        "name": t.card.name,
        "description": t.card.description,
        "parameters": t.card.parameters,
    }
}
```

默认 17 个工具：`web_fetch`、`web_search`、`command_exec`、`read_file`、`write_file`、`edit_file`、`list_files`、`glob`、`todo_create`、`todo_complete`、`todo_list`、`list_skill`、`read_skill`、`memory_search`、`write_memory`、`read_memory`、`edit_memory`。

---

## 8. 工具执行后的消息回填

当 LLM 返回 `finish_reason == "tool_calls"` 时：

1. **assistant 消息入库**：包含 `tool_calls` 的完整 assistant_message append 到 SessionStore
2. **逐个工具执行**：每个 tool_call 经过 `_hooked_tool_call(ctx)`：
   - `PermissionHook`（priority=100）在 `before_tool_call` 检查权限：ALLOW→继续、DENY→`request_force_finish`（返回拒绝消息作为 tool_result）、ASK→`HookInterrupt`（暂停等待人机审批）
   - 工具执行结果 append 到 SessionStore：
     ```python
     {"role": "tool", "tool_call_id": tc["id"], "content": result}
     ```
3. **todo 事件广播**：`flush_todo_events()` 把 todo 状态变更作为 `e2a.todo_update` 帧推给前端
4. **`_reask = True`**：外层循环进入下一个 ReAct 步骤，重新从 ② 开始拉取消息（此时 session 历史已包含新的 assistant + tool 消息）

---

## 9. 最终消息结构全景图

下面是一次典型 LLM 调用发送给 OpenAI API 的完整消息结构：

```
messages:
┌─────────────────────────────────────────────────────────────┐
│ [0] system — 合并后的唯一 system 消息                        │  ← _merge_system_messages
│     "# 身份与行为原则..." (身份 ← 开头注意力热点)            │
│     "## 可用技能..." (技能 ← 身份之后)                       │
│     "## 长期记忆..." (记忆 ← 技能之后)                       │
│     "[prior context summary] ..." (压缩摘要 ← 如有，靠近对话) │
│                                                              │
│ [1] user — 第一轮用户输入                                    │  ← SessionStore
│ [2] assistant — 第一轮模型回复 / tool_calls                  │  ← SessionStore
│ [3] tool — 第一轮工具结果                                    │  ← SessionStore
│ ...                                                          │  ← SessionStore
│ [N-1] assistant — 最近一轮模型回复 / tool_calls              │  ← SessionStore
│ [N]   user — 当前请求的 query                                │  ← 本次 append
└─────────────────────────────────────────────────────────────┘

tools:
┌─────────────────────────────────────────────────────────────┐
│ 17 个 OpenAI function-calling schema                         │  ← ToolManager.schemas()
│ (web_fetch, command_exec, todo_create, memory_search, ...) │
└─────────────────────────────────────────────────────────────┘
```

**无压缩时**，合并后的 system 消息不含摘要段。

---

## 10. 设计原则总结

| 原则 | 体现 |
|------|------|
| **无损历史、有损视图** | `history.json` 保存完整对话；压缩只塑造 LLM 的即时视角，不回写 store |
| **新列表赋值、不原地修改** | 压缩、SkillHook、MemoryHook 都用 `[新消息] + ctx.inputs.messages` 赋新 list，避免污染 store 内部缓存 |
| **合并多条 system 为一条，身份在前** | Hook prepend 后 `_merge_system_messages()` 把所有头部 system 消息合并成一条，按身份→技能→记忆→摘要排序——身份占开头注意力热点，操作指南靠近对话利用 recency bias（对齐 jiuwenswarm 的 SystemPromptBuilder） |
| **Hook prepend 不变，合并后处理排序** | Hook 仍用 prepend 注入（不改 Hook 代码），合并步骤负责最终排序——Hook 和排序解耦 |
| **工具 schema 与消息分离** | 工具定义通过 `tools` 参数传递，不嵌入消息体。LLM 同时看到消息上下文和可用工具列表 |
| **优雅降级** | 压缩摘要失败→丢弃中间段(head+tail)；空 store→MemoryHook no-op；无 skill→SkillHook no-op |
| **orphan 修补保证合约** | 每次请求入口扫描上次崩溃遗留的未配对 tool_calls，补合成结果 |
| **ContextVar 避免参数透传** | `session_id`/`channel` 通过 ContextVar 绑定，工具函数无需逐层传递 |

---

## 11. 配置参数一览

| 参数 | 配置文件位置 | 默认值 | 作用 |
|------|------------|--------|------|
| `context_compression.token_threshold` | `config.yaml` | 60000 | 压缩触发阈值（char // 3 估算） |
| `context_compression.keep_recent_pairs` | `config.yaml` | 6 | 保留最近 N 个 user/assistant 对（×2 条消息） |
| `context_compression.summary_prompt` | `config.yaml` | 中文压缩器 prompt | 摘要 LLM 调用的 system prompt |
| `skills.mode` | `config.yaml` | `"all"` | `"all"`=每步注入完整清单；`"auto_list"`=只注入提示 |
| `skills.enabled` | `config.yaml` | `[]`（全开） | 白名单；空=所有 skill 可用 |
| `agent.max_steps` | `config.yaml` | 1000 | ReAct 最大步数 |
| `permissions.enabled` | `config.yaml` | false | false=全 ALLOW，无审计 |
| `permissions.tools.command_exec` | `config.yaml` | require-approval | command_exec 需人机审批 |

---

## 12. 关键源码索引

| 阶段 | 文件 | 行号 | 关键函数/类 |
|------|------|------|-------------|
| 请求入口 | `agent_loop.py` | 81-119 | `AgentLoop.run_stream()` |
| 会话初始化 | `agent_loop.py` | 135-152 | `_inner_run_stream()` 前 20 行 |
| Orphan 修补 | `agent_loop.py` | 320-348 | `_sanitize_orphan_tool_calls()` |
| 系统提示词构建 | `agent_loop.py` | 47-110 | `build_system_prompt()` |
| System 消息合并 | `agent_loop.py` | 425-470 | `_merge_system_messages()` |
| 消息拉取 | `sessions/store.py` | 181-189 | `SessionStore.get_messages()` |
| 消息字段过滤 | `sessions/store.py` | 31 | `_OPENAI_FIELDS` |
| Token 估算 | `context_compression.py` | 14-35 | `estimate_tokens()` |
| 三段分割 | `context_compression.py` | 38-56 | `_split_keep_tool_pairs()` |
| LLM 摘要 | `context_compression.py` | 75-86 | `_summarize()` |
| 压缩主函数 | `context_compression.py` | 89-114 | `compress_messages()` |
| Hook 框架 | `hooks/base.py` | 50-107 | `AgentHook`、`HookContext`、`ModelCallInputs` |
| Hook 调度 | `hooks/manager.py` | 80-98 | `HookManager.execute()` |
| SkillHook | `hooks/builtin/skill_hook.py` | 17-44 | `SkillHook.before_model_call()` |
| MemoryHook | `hooks/builtin/memory_hook.py` | 28-46 | `MemoryHook.before_model_call()` |
| LLM 调用 | `llm_client.py` | 42-120 | `LLMClient.stream()` |
| 工具 schema | `tools/manager.py` | 31-42 | `ToolManager.schemas()` |
| 工具注册 | `tools/__init__.py` | 17-37 | `tool_manager()` |
| 配置默认值 | `resources/config.yaml` | 全文 | YAML 配置 |
