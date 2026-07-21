# Todo 进度可见性(UI) — 设计文档

> 日期: 2026-07-21
> 阶段: Phase 2 后续(任务规划 UI 可见性)
> 前置: 已合并的 task-planning 后端(`TodoStore` + 3 个 `@tool` + ContextVar 路由 + system message)
> 参考: jiuwenclaw `agentserver/tools/todo_toolkits.py` 的 `_publish_op_result` + 前端 todo 面板

## 1. 目标与范围

后端 todo 闭环已通,但 `todo_create`/`todo_complete` 的执行结果只在 agent 内部回灌进 store,**不流到浏览器**——页面看不到任务拆解与进度。本设计补上「todo 状态实时流到前端、渲染成侧边面板」这一层。

### 明确做

- 后端:todo 工具执行时往 per-request 事件 list publish 一条结构化快照;`agent_loop` drain 后 yield `e2a.todo_update` frame;`MessageHandler` 按 `response_kind` 分支,emit `todo.update` 浏览器事件。
- 前端:`webClient.ts` 加 `todo.update` 事件处理;`App.vue` 右侧加 TodoPanel(checkbox 列表 + N/M 进度),`todo.update` 来一条整体替换。

### 明确不做(YAGNI / 砍)

- **不建 rail 框架**:只接回 op-result 总线的 publish 半边,消费者是 `agent_loop`,不引入 jiuwenclaw 的 rail 系统。
- **不加 `todo_clear` / 新请求自动清空**:列表会话级累积(对齐 in-memory store);模型在新轮 `todo_create` 会因「已存在」失败——UI 如实反映,后续再议。
- **不流式普通工具调用**:只对 todo 工具 publish 侧事件,其它工具(web_fetch/command_exec 等)不显示执行进度。
- **不动 `ToolManager.execute -> str` 契约**:结构化数据走侧信道,不破工具返回类型。
- **前端不写单测**:项目前端无测试基建(现状),手动验证。
- **不改 chat 渲染**:delta/final 链路完全不动,todo 面板是独立侧栏。

## 2. 架构与组件

```
twinkle/agentserver/
  plan_todo_context.py   ← + per-request 事件 list ContextVar + publish/drain helpers
  tools/todo_tools.py     ← create/complete 执行后 publish 结构化快照
  agent_loop.py          ← 每次 tools.execute 后 drain 事件 list → yield e2a.todo_update
  e2a/models.py          ← response_kind 注释加 e2a.todo_update(str 字段,无 schema 改动)
twinkle/schema/
  message.py             ← EventType.TODO_UPDATE = "todo.update"
twinkle/gateway/
  message_handler.py     ← _process_stream 按 response_kind 分支
web/src/
  services/webClient.ts  ← + onTodoUpdate handler
  App.vue                ← + 右侧 TodoPanel
```

### 组件职责

- **`plan_todo_context.py`** 新增(保留原 `PLAN_TODO_SESSION_ID` / `get_plan_todo_session_id`):
  - `TODO_EVENTS: ContextVar[list[dict]]`(default `None`,语义:None=无 bus,[] = 空 bus)。
  - `reset_todo_events()`:`TODO_EVENTS.set([])`(`run_stream` 入口调,per-request 初始化)。
  - `publish_todo_update(snapshot: dict)`:取当前 list;若为 `None`(bus 未初始化)则 no-op(工具在 loop 外被调用时不崩);否则 append。
  - `drain_todo_events() -> list[dict]`:取当前 list,`set([])` 清空,返回旧 list。
- **`todo_tools.py`**:`todo_create` / `todo_complete` 在成功路径(拿到 `created`/`tasks` 后)调 `publish_todo_update({"tasks": [...], "remaining": n, "total": m})`。`todo_list` **不 publish**(只读,不改变状态;且 list 本身已被 create/complete 的 publish 带出)。snapshot 的 `tasks` 元素形如 `{"idx":1,"title":"...","status":"waiting|completed","result":"..."}`,对齐 `TodoTask` dataclass。
- **`agent_loop.py`**:在 tool 执行循环里(现有 `for tc in tcs` 块),每次 `await self._tools.execute(name, args)` 之后,加 `for ev in drain_todo_events(): yield E2AResponse(response_kind="e2a.todo_update", body=ev, is_final=False, ...)`。这些 frame 在 append tool 结果到 store、`continue` 之前 yield 出去。
- **`e2a/models.py`**:`E2AResponse.response_kind` 注释从 `e2a.chunk | e2a.complete | e2a.error` 扩到 `| e2a.todo_update`。字段类型仍是 `str`,无 schema 变更。
- **`schema/message.py`**:`EventType.TODO_UPDATE = "todo.update"`。
- **`message_handler.py`**:`_process_stream` 改为按 `response_kind` 分支:
  - `e2a.todo_update` → `Message(event_type=EventType.TODO_UPDATE, payload=resp.body, content="")`;
  - 其它 → 原逻辑(`content = resp.body.result.content`,`is_final` → CHAT_FINAL)。
  - 关键:`todo.update` 的数据走 `Message.payload`(结构化 dict),**不**塞进 `content`(那是给 chat 文本用的)。
- **`webClient.ts`**:加 `TodoUpdateHandler = (todo: {tasks:[], remaining:number, total:number}, rid:string) => void`;`setHandlers(onDelta, onFinal, onTodoUpdate)`;`handle` 里 `frame.event === 'todo.update'` → `this.onTodoUpdate(frame.payload, rid)`。
- **`App.vue`**:加 `const todo = ref<{tasks:[], remaining:number, total:number} | null>(null)`;`onTodoUpdate` 回调里 `todo.value = payload`;模板加 `<aside class="todo-panel">` 渲染。layout:`.chat` 改 flex 两栏。

## 3. 数据流

多步请求(todo_create → todo_complete → answer):

```
E2AEnvelope(session_id, query)
  → run_stream:
      1. PLAN_TODO_SESSION_ID.set(session_id)
      2. reset_todo_events()                    ← per-request bus 初始化
      3. [首次] store.append(system_message)
      4. store.append(user)
      5. for step:
           llm.stream(msgs, tools) → Finish(tool_calls=[todo_create])
             for tc:
               result = tools.execute("todo_create", {tasks})
                 └─ todo_create 内部:store.create → publish_todo_update(snapshot)
               for ev in drain_todo_events():    ← 新增
                 yield E2AResponse(response_kind="e2a.todo_update", body=ev)
               store.append({role:tool, content:result})   ← 字符串照旧给模型
             continue
           ... (todo_complete 同理再 publish + drain + yield) ...
           llm.stream → Finish(stop): yield e2a.complete

agentserver ws → gateway:
  MessageHandler._process_stream:
    resp.response_kind == "e2a.todo_update"
      → Message(event_type=TODO_UPDATE, payload=resp.body)  ← 结构化
    else → chat.delta/chat.final(原逻辑)
  → ChannelManager → WebChannel.send → 广播 {type:event, event:"todo.update", payload}

浏览器:
  webClient.handle → onTodoUpdate(payload) → App.vue todo.value = payload
  → TodoPanel 重渲染:[x]/[ ] 列表 + N/M 进度
```

## 4. 错误处理

- **bus 未初始化**:`publish_todo_update` 检测 `TODO_EVENTS.get() is None` → no-op(不 append、不抛)。这样:工具在 `run_stream` 外被单测直接 invoke(不经 loop)时不会崩;只有 loop 内才真正 publish。
- **drain 在 bus 未初始化时**:`drain_todo_events` 返回 `[]`(get None → [])。
- **gateway 分支失败**:`MessageHandler._process_stream` 的 try/except 已兜底(emit CHAT_FINAL error)。todo_update 分支不引入新异常路径。
- **前端 payload 形状不符**:面板用可选链(`todo.value?.tasks ?? []`),缺字段渲染空列表,不崩。
- **工具层业务错误**(create 已存在 / complete not found):走原 `TodoError` catch,**不 publish**(只有成功路径 publish)——错误信息作为字符串回给模型,前端面板不动。

## 5. 测试

`asyncio.run()` + 无 pytest-asyncio。

- **`tests/test_plan_todo_context.py` 扩展**:publish/drain——`reset_todo_events()` 后 publish 两条 → drain 返回两条且 bus 清空;`drain_todo_events()` 在未 `reset` 时返回 `[]`;`publish_todo_update` 在未 reset 时 no-op(不抛、不污染)。
- **`tests/test_todo_tools.py` 扩展**:`todo_create` 成功后 `drain_todo_events()` 含一条 snapshot(`tasks` len 正确、`remaining`/`total` 正确);`todo_complete` 成功后 snapshot 反映 completed 状态;`todo_list` 不 publish(drain 为空);`todo_create` 失败路径(已存在)不 publish。
- **`tests/test_agent_loop.py` 扩展**:在现有 `test_todo_create_round_trip_through_loop` 基础上,断言 frames 里含一个 `response_kind=="e2a.todo_update"` 且 `body.tasks` 长度 == 2、`remaining==2`、`total==2`;再加一个 `todo_complete` 后 update frame 的用例(完成数 == 1)。
- **`tests/test_message_handler.py`(新建)**:fake `AgentClient.send_request_stream` yield 一条 `E2AResponse(response_kind="e2a.todo_update", body={...})` → 验证 MessageHandler `dequeue_outbound()` 出来的 `Message.event_type == EventType.TODO_UPDATE`、`payload == body`、`content == ""`。再加一条 yield `e2a.chunk` 的确认原 chat.delta 路径不受分支改动影响。
- 前端:无单测;手动 `npm run dev` + 触发 `todo_create` 验证面板出现与刷新。

## 6. 对齐与偏离

| 维度 | jiuwenclaw | Twinkle | 理由 |
|---|---|---|---|
| 事件发布 | TodoToolkit `_publish_op_result` → rail 消费 | todo 工具 publish → loop drain | 砍 rail;loop 当消费者,等价语义 |
| 事件结构 | TodoOpResult(kind/success/remaining/total/all_completed) | {tasks, remaining, total} | Twinkle 前端要渲染列表,带 tasks 更直接;kind/success 对 UI 非必需 |
| 浏览器事件 | rail → stream_event_rail → 浏览器 | E2AResponse → MessageHandler → todo.update | 两段翻译一致,无 rail |
| 前端 | 完整 todo 面板组件 | App.vue 内联侧栏 | Twinkle 前端极简,单组件够用 |
| 清空 | 有 rail/会话管理 | 无 | YAGNI;会话级累积,后续加 todo_clear |

## 7. 验收

- `todo_create` 执行时,页面右侧面板立即出现任务列表 + `0/N` 进度(在模型还没回最终答案之前)。
- `todo_complete` 执行时,对应条目变 `[x]` + 显示 result,头部进度 `N/M` 递增。
- 一句话简单任务(不调 todo 工具):面板保持「暂无任务」,不受影响。
- 多 session:浏览器单连接单 session,不涉及跨 session;面板只反映当前 session 的 todo。
- 全量后端测试绿(新增 bus/tools/loop/message_handler 用例),无 pytest-asyncio。
