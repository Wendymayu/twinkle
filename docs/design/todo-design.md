# Todo 系统设计

## 一句话概括

Agent 用三个工具（create / complete / list）规划多步任务，状态变更通过 side-channel 实时推送到前端 TodoPanel。

---

## 为什么需要 Todo

ReAct 循环让 agent 能边想边做，但没有结构化的进度表示——用户只能看到一串流式文本。对于"帮我调研 A、对比 B、总结 C"这类多步请求，agent 需要一种方式：

1. **提前规划**：把大任务拆成子步骤，让用户看到全貌。
2. **逐步推进**：完成一步就标记一步，不会遗漏或重复。
3. **实时反馈**：前端 TodoPanel 同步更新，用户不用等到最后才知道进度。

---

## 数据模型

```
TodoTask
  idx: int          # 1-based 序号，工具层直接用
  title: str        # 子任务描述
  status: str       # "waiting" | "running" | "completed"
  result: str       # 完成时的简短结果，默认 "done"
```

一个 session 最多一条 todo 列表；`create` 会拒绝已有列表的 session（防止重复规划）。

---

## 三个工具

| 工具 | 输入 | 返回 | 说明 |
|---|---|---|---|
| `todo_create` | `tasks: list[str]` | markdown + 当前列表 | 创建整条 todo 列表；已存在则报错 |
| `todo_complete` | `idx: int, result?: str` | markdown + 当前列表 | 标记完成；已完成或 idx 不存在则报错 |
| `todo_list` | _(无参数)_ | markdown 列表 | 查看当前进度 |

返回值是 markdown 字符串，直接作为 tool_result 回给模型——agent 不需要额外调用 `todo_list` 就能看到最新状态。

错误用 `TodoError`（业务级异常，消息直接可读），不会炸掉 ReAct 循环。

---

## Session 路由：ContextVar

问题：todo 工具是无参的（模型不需要传 session_id），但 TodoStore 按 session 隔离。

解法：`PLAN_TODO_SESSION_ID` — 一个 `ContextVar[str]`。

```
AgentLoop.run_stream 入口
  → PLAN_TODO_SESSION_ID.set(envelope.session_id)

todo_tools 内部
  → get_plan_todo_session_id()  # 取 ContextVar，fallback "default"
```

这样工具函数零参数，agent 也不用操心 session 路由。ContextVar 在 asyncio 里天然 per-task 隔离，并发请求互不干扰。

---

## Side-channel：从 Store 到 UI

Todo 状态变更不只回给模型，还要推送到前端。"Side-channel"（旁路）的意思是：todo 状态更新走了一条与聊天流并行但独立的管道——它们共享同一条 ws 连接和同一个 `request_id`，但 `response_kind` / `event` 不同，前端按 kind 分流。

| 通道 | 内容 | 帧类型 | 前端目标 |
|---|---|---|---|
| **主通道** | 模型输出的文本 | `e2a.chunk` → `chat.delta` | 聊天气泡 |
| **旁路** (side-channel) | todo 状态快照 | `e2a.todo_update` → `todo.update` | TodoPanel |

为什么叫"旁路"而不把 todo 信息直接塞进模型文本里：

1. **模型不需要** — tool_result 已经是 markdown，agent 能读；前端需要的是结构化的 `{tasks, remaining, total}` JSON，两者受众不同。
2. **不污染聊天流** — todo 进度是 UI 层的辅助信息，不该混在用户看到的对话文本里。
3. **实时性** — `publish_todo_update` 在工具执行时立即写入 ContextVar，`agent_loop` 在下一次 yield 前就捞出去，不需要等整轮 ReAct 结束。

类比：主通道是公路，side-channel 是公路旁边的自行车道——同方向同行，但服务不同交通工具。

整条数据流：

```
todo_tools.py
  → append_todo_event(snapshot)       # 写 ContextVar list
  → flush_todo_events()                  # AgentLoop 在 yield 间隙捞出

agent_loop.py
  → E2AResponse(response_kind="e2a.todo_update", body=snapshot)

gateway/message_handler.py
  → Message(event="todo.update", payload=snapshot)

web/webClient.ts
  → onTodoUpdate → todo.value = snapshot

TodoPanel.vue
  → 渲染 tasks / completedCount / ○◐✓ 图标
```

**关键设计**：`TODO_EVENTS` ContextVar 是 per-request 的临时缓冲区，`run_stream` 入口 `reset_todo_events()` 清空，工具 `append_todo_event()` 入队，`agent_loop` 在每次 tool 执行后 `flush_todo_events()` 取出并 yield `e2a.todo_update` 帧——那才是真正的"发布"。

snapshot 结构：

```json
{
  "tasks": [{"idx": 1, "title": "...", "status": "waiting", "result": ""}],
  "remaining": 2,
  "total": 3
}
```

前端用 `remaining / total` 显示进度摘要，用 `tasks` 渲染列表。

### 前端渲染策略：全量替换

前端收到 `todo.update` 事件后，直接丢弃旧值，用最新 snapshot 替换整个 `todo` ref：

```ts
// useSessions.ts
(t) => { todo.value = t }   // 一条赋值，全量替换
```

Vue 响应式系统检测到 `todo.value` 变化，触发 TodoPanel 重渲染。不需要 patch 逻辑、不需要记住上一条 snapshot——每次推送都是完整状态，一个赋值就够了。

选择全量替换而非增量更新的原因：

- 典型 todo 列表 3–5 条，snapshot 体量很小，全量推送开销可忽略
- 全量替换零出错：不担心丢帧、乱序、或 patch 逻辑 bug
- 实现极简：一条赋值语句，前端不需要任何 diff/merge 逻辑

---

## 存储策略

**纯内存**，不持久化。与 SessionStore 的"磁盘 + 缓存"策略不同，原因：

- Todo 是短期规划辅助，session 结束就失效。
- 内存存储让 `create` 的"已有列表"判定天然正确——重启后列表自然消失。
- 与 jiuwenclaw 的 todo.md 文件不同，Twinkle 不需要跨进程共享 todo。

TodoStore 用 `dict[session_id, list[TodoTask]]` + 每 session 一把 `asyncio.Lock` 串行化 read-modify-write。

---

## 系统提示注入

`AgentLoop._inner_run_stream` 在 session 首次请求时插入一条 system message：

```
"You have todo tools to plan and track multi-step work: todo_create,
 todo_complete, todo_list. For non-trivial multi-step requests, first
 call todo_create … For simple one-step requests, do NOT use the todo
 tools — just answer or call the needed tool directly."
```

只插入一次（检查 messages[0] 是否已有 system role），避免每次请求重复注入浪费 token。

---

## 文件地图

| 文件 | 角色 |
|---|---|
| `agentserver/todo/store.py` | TodoStore + TodoTask + TodoError |
| `agentserver/todo/context.py` | ContextVar：session 路由 + 事件缓冲区 |
| `agentserver/todo/__init__.py` | 包入口 re-export |
| `agentserver/tools/builtin/todo_tools.py` | 三个 @tool 函数 |
| `agentserver/agent_loop.py` | 入口 set ContextVar + drain events + insert system prompt |
| `e2a/models.py` | `response_kind` 包含 `e2a.todo_update` |
| `gateway/message_handler.py` | `e2a.todo_update` → `todo.update` 事件翻译 |
| `web/src/composables/useSessions.ts` | `todo` ref + `completedCount` computed |
| `web/src/components/TodoPanel.vue` | UI 渲染 |

---

## 与 jiuwenclaw 的差异

| | jiuwenclaw | Twinkle |
|---|---|---|
| 工具数 | 7 (start/insert/remove/batch…) | 3 (create/complete/list) |
| 存储 | todo.md 文件 | 纯内存 dict |
| 事件发布 | op-result 总线 | ContextVar 缓冲区 + agent_loop yield |
| session 路由 | team session 解析 | ContextVar 直取 session_id |

砍掉 start/insert/remove/batch 是因为 Twinkle 的 agent 场景更简单：一次性规划、顺序推进，不需要动态插入或批量操作。
