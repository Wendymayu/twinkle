# Task Planning (Phase 2 收尾) — 设计文档

> 日期: 2026-07-21
> 阶段: Phase 2 收尾(M3「能管工具 = 多工具选择 + 任务规划」的规划半)
> 参考: jiuwenclaw `agentserver/tools/todo_toolkits.py` + `agentserver/plan_todo_context.py`

## 1. 目标与范围

Twinkle 当前 `agent_loop` 是扁平 ReAct:模型每步看着完整历史自己决定下一步,无显式任务拆解与跟踪。本设计为它加上 jiuwenclaw 风格的**轻量任务规划能力**——常驻注册 todo 工具 + 一段 system prompt 引导,由模型自己判断是否拆任务。**不建复杂度分类器**(jiuwenclaw 也没有),简单任务模型自然不调。

### 明确做

- 3 个 todo 工具:`todo_create` / `todo_complete` / `todo_list`,用 `@tool` 装饰,经 `ToolManager` 注册,`agent_loop` 经现有 `schemas()`/`execute()` 调用,**loop 零改动**。
- 内存 `dict[session_id, list[TodoTask]]` 存储,带 `asyncio.Lock`。
- session 路由用 `ContextVar`(对齐 jiuwenclaw `plan_todo_context`),`agent_loop.run_stream` 入口 set,工具读取。不改 `Tool` 接口签名。
- 会话首次插入一条 system message,讲清 todo 工具的用法与「简单任务不要用」。

### 明确不做(YAGNI / 砍)

- `todo_start` / `todo_insert` / `todo_remove`:砍。`TodoTask.status` 字段保留 `running` 取值供将来 `start` 工具,但现在不暴露入口。
- `todo_complete_batch`:砍。
- op-result 发布总线(`_publish_op_result` / `consume_last_op_result`):砍。它只服务 jiuwenclaw 的 rail 消费,Twinkle 无 rail 系统,留着是死重量。
- 磁盘持久化 / markdown 文件:砍。与 Twinkle 内存 SessionStore 哲学一致,roadmap 明确持久化/长期记忆推迟。`TodoStore` 接口形态允许后续换实现。
- 复杂度分类器 / 规划引擎:不做。对齐 jiuwenclaw「能力常在、用不用看模型」的设计。
- `clear(session_id)`:先不加。测试需要隔离时用独立 session_id 即可。

## 2. 架构与组件

```
twinkle/agentserver/
  plan_todo_context.py   ← ContextVar: 当前请求 session_id(对齐 jiuwenclaw 同名)
  todo_store.py          ← TodoStore: 内存 dict[session_id -> list[TodoTask]] + asyncio.Lock
  tools/todo_tools.py    ← @tool 装饰的 3 个 async 函数: create/complete/list
                           内部读 ContextVar 拿 session_id,操作 TodoStore
  tools/__init__.py      ← tool_manager() 注册 3 个 todo 工具
  agent_loop.py          ← run_stream 入口 set ContextVar + 会话首次插 system message
```

### 组件职责

- **`plan_todo_context.py`**:`PLAN_TODO_SESSION_ID: ContextVar[str]`(default `"default"`) + `get_plan_todo_session_id() -> str`。取不到返回 `"default"`,不抛异常。镜像 jiuwenclaw `plan_todo_context.py:33-45`。
- **`todo_store.py` — `TodoStore`**:
  - `TodoTask` dataclass:`idx: int, title: str, status: str, result: str`。`status ∈ {"waiting","running","completed"}`。
  - `async create(session_id, tasks: list[str]) -> list[TodoTask]`:该 session 已有列表 → 抛业务错误(由工具层捕获转字符串);否则建 `TodoTask(idx=i+1, title=t, status="waiting", result="")`。
  - `async complete(session_id, idx, result="") -> list[TodoTask]`:idx 不存在/已完成 → 业务错误;否则置 `status="completed", result=result or "done"`。
  - `async list(session_id) -> list[TodoTask]`:空列表正常返回 `[]`。
  - 内部 `dict + asyncio.Lock`:同 session 的 read-modify-write 串行,防丢更新。跨 session 天然隔离。
- **`tools/todo_tools.py`**:3 个 `async def` + `@tool`,签名/类型/docstring 自动派生 schema(与 `web_fetch`/`web_search`/`command_exec` 一致)。每个函数:`sid = get_plan_todo_session_id()`,调 `TodoStore`,把结果格式化成 markdown 串返回(返回串附带当前列表,省一次 `todo_list` round-trip)。业务错误 try/except 转成 `Error: ...` 字符串返回(不抛出去,虽然 `ToolManager.execute` 也兜底,但工具层自己转更可读)。
- **`tools/__init__.py` `tool_manager()`**:在现有 3 个只读工具后 `tm.register(tool(todo_tools.todo_create))` 等。
- **`agent_loop.py` `run_stream`**:开头两步——(1) `PLAN_TODO_SESSION_ID.set(envelope.session_id or "default")`;(2) 会话首次插 system message:检查 `self._store.get_messages(session_id)` 首条是否已是 system role,否就 `store.append(session_id, {role:"system", content: TODO_SYSTEM_PROMPT})`。`TODO_SYSTEM_PROMPT` 常量放 `agent_loop.py` 模块级(不新建 `prompts.py`,避免单常量开文件)。

## 3. 数据流

多步请求路径:

```
E2AEnvelope(session_id, query)
  → run_stream:
      1. PLAN_TODO_SESSION_ID.set(session_id)
      2. [会话首次] store.append(system_message)
  3. store.append({role:user, content:query})
      4. for step in MAX_STEPS:
           llm.stream(msgs, tools=tools.schemas())   # schemas() 现含 3 个 todo 工具
           ├─ TextDelta → yield e2a.chunk
           └─ Finish:
                if tool_calls:
                    for tc: result = tools.execute(name, args)
                        └─ todo_* 读 ContextVar → TodoStore → 返回 markdown 串
                    store.append({role:tool, tool_call_id, content:result})
                    continue   # 带 todo 结果再问模型
                else: yield e2a.complete; return
```

工具结果回灌复用 `agent_loop.py:68-77` 现有机制(把 `role:tool` append 进 store,下一步 `get_messages` 带上)。`todo_create` 返回的「Created N tasks + current todo list」进 store,模型下一轮看得见拆解结果。

## 4. 错误处理

- 工具失败不崩 loop:`ToolManager.execute`(`manager.py:44-51`)已 try/except 兜底返回 `[tool error] ...`。todo 工具层自己再把业务级错误转成 `Error: ...` 字符串返回模型(双重保险,且工具层字符串更可读)。
- 业务错误(返回字符串,非异常):
  - `todo_create` 已有列表 → `Error: todo list already exists for session X.` + 当前列表。
  - `todo_complete` idx 不存在 → `Error: Task N not found.`;已完成 → `Error: Task N is already completed.`
  - `todo_create` 空 `tasks=[]` → `Error: tasks must be a non-empty list.`
- ContextVar 兜底:`get_plan_todo_session_id()` 取不到返回 `"default"`,不抛异常(graceful 退化)。
- 返回串附带当前 todo 列表(对齐 jiuwenclaw `_append_todo_list`)。

## 5. 测试

按 Twinkle 约定:**不用 pytest-asyncio**,用 `asyncio.run()` + `tests/conftest.py` 的 `free_port`/`port_factory`(todo 工具单测不需要起 ws)。

- `tests/test_todo_store.py`:
  - create → list 正确;
  - 重复 create 报业务错误;
  - complete 不存在/已完成 idx 报错;
  - complete 后 status/result 正确;
  - 并发交错不丢更新(同 session 两协程)。
- `tests/test_todo_tools.py`:
  - ContextVar set 不同 session_id,验证隔离(A 的 todo B 看不到);
  - `@tool` 派生 schema 形状(name/description/parameters)正确;
  - 工具返回串含当前列表。
- 不写 e2e ws 测试:todo 工具经 `ToolManager.execute` 覆盖,ws 链路 Phase 0/1 已验。
- 可选:`tests/test_agent_loop.py` 加一个 fake-LLM 用例,验证 run_stream 开头确实插了 system message 且 ContextVar 被设(轻量,确认 wiring)。

## 6. 对齐与偏离说明

| 维度 | jiuwenclaw | Twinkle | 理由 |
|---|---|---|---|
| 工具集 | 6 + batch | 3(create/complete/list) | YAGNI;主路径够用 |
| 存储 | 磁盘 `todo.md` | 内存 dict | 对齐 SessionStore 哲学;持久化 roadmap 推迟 |
| session 路由 | ContextVar | ContextVar | 一致,不改 Tool 接口 |
| op-result 总线 | 有(rail 消费) | 无 | Twinkle 无 rail,死重量 |
| prompt 注入 | rail 每次 before_model_call 注入 | 会话首次插 system message | Twinkle 无 rail 框架;首次插避免堆积 |
| 复杂度判断 | 无 | 无 | 一致,对齐 jiuwenclaw |

## 7. 验收(对齐 M3 规划半)

- 多步任务(如「调研 X 并对比 Y」):模型用 `todo_create` 拆步、逐步用 `todo_complete` 标记、`todo_list` 查进度,全程 loop 不崩、结果回灌。
- 一句话简单任务(如「今天上海天气」):模型不调任何 todo 工具,直接走 tool call 一步完成——**无分类器,纯模型自行判断**。
- 多 session 隔离:A session 的 todo 列表 B session 看不到。
- 全部测试用 `asyncio.run()`,无 pytest-asyncio 依赖。
