# Phase 8 — Todo 增强（Task 级别规划追踪）

> 日期：2026-08-01
> 状态：设计完成，待实现

## 1. 目标

将现有 Todo 系统从"扁平清单"增强为"结构化任务追踪"，对齐 jiuwenswarm 的 TodoItem（id、depends、claimedBy）+ Claude Code 的 TaskCreate（description、metadata），用一套系统覆盖简单和复杂场景。

## 2. Todo vs Task 的区别

| 维度 | Todo（清单） | Task（结构化追踪） |
|---|---|---|
| **标识** | 无 ID，靠位置 idx | 有唯一 ID |
| **状态** | 3 态：waiting/running/completed | 4 态：pending/in_progress/completed/cancelled |
| **依赖** | 无 | blocked_by（线性依赖用 sequential=True 一步搞定） |
| **归属** | 无 | owner |
| **描述** | 只有 title | subject + description |
| **元数据** | 无 | metadata（任意 KV） |
| **操作模式** | 批量创建 | 批量创建 + 逐个更新 |
| **执行保证** | 无 | 轻量守卫（跳步检测 + 提醒） |

**核心区别**：Todo 是 LLM 的笔记本，Task 是 LLM 的项目管理器。增强后一套系统同时满足两种场景。

## 3. 为什么不新增独立 Task 系统

Claude Code 有两套（TodoWrite + TaskCreate），因为 TodoWrite 设计太简陋（无 ID、全量替换）无法扩展，只能另起炉灶。jiuwenswarm 只有一套——它的 TodoItem 已经包含 id、depends、claimedBy，不需要两套。

Twinkle 选择 jiuwenswarm 的路线：**一套系统，增强 Todo 到 Task 级别**。工具名保持 `todo_*`，不改名。

## 4. 数据模型

### 4.1 增强前

```python
@dataclass
class TodoTask:
    idx: int
    title: str
    status: str  # "waiting" | "running" | "completed"
    result: str = ""
```

### 4.2 增强后

```python
@dataclass
class TodoTask:
    id: str                                    # UUID
    subject: str                               # 标题（原 title）
    description: str = ""                      # 详细描述
    status: str = "pending"                    # pending | in_progress | completed | cancelled
    result: str = ""
    blocked_by: list[str] = field(default_factory=list)  # 依赖的 task id
    owner: str = ""                            # 归属
    metadata: dict = field(default_factory=dict)          # 任意 KV
    created_at: float = 0.0
    updated_at: float = 0.0
```

### 4.3 变更说明

- `idx: int` → `id: str`（UUID，全局唯一）
- `title` → `subject`（对齐 Claude Code 命名）
- 新增 `description`（详细描述，可选）
- 状态：`waiting` → `pending`，`running` → `in_progress`，新增 `cancelled`
- 新增 `blocked_by`（依赖的 task id 列表）
- 新增 `owner`（归属）
- 新增 `metadata`（任意 KV，merge-style 更新）
- 新增 `created_at` / `updated_at`（时间戳）
- 不加 `active_form`（in_progress 时直接显示 subject，前端自动加 spinner）

## 5. 工具集

### 5.1 增强前

| 工具 | 参数 | 说明 |
|---|---|---|
| `todo_create` | `tasks: list[str]` | 批量创建 |
| `todo_complete` | `idx: int, result: str` | 标记完成 |
| `todo_list` | 无 | 列出任务 |

### 5.2 增强后

| 工具 | 参数 | 说明 |
|---|---|---|
| `todo_create` | `subjects: list[str], sequential: bool = False` | 批量创建；`sequential=True` 自动串联线性依赖 |
| `todo_update` | `task_id: str, status?, result?, owner?, metadata?` | 通用更新（完成是 `status="completed"`） |
| `todo_list` | `status?: str` | 列出任务（可按状态过滤） |
| `todo_get` | `task_id: str` | 获取单个任务详情 |

### 5.3 关键变更

1. **`todo_create`**：`tasks: list[str]` → `subjects: list[str]`；新增 `sequential=True` 自动为每个 task 设置 `blocked_by` 指向前一个 task；返回创建的 tasks（含真实 ID）
2. **`todo_complete(idx, result)`** → 合并进 `todo_update(task_id, status="completed", result=...)`
3. **新增 `todo_get`**：按 id 查单个任务
4. **`todo_update` 的 `metadata`**：merge-style 更新（设置 key 为 null 删除，对齐 Claude Code）

### 5.4 `sequential=True` 的设计

`sequential=True` 是本次设计的关键创新，解决"一次创建 + 设依赖"的问题：

```python
todo_create(subjects=["步骤A", "步骤B", "步骤C"], sequential=True)
# → 自动：B.blocked_by=[A.id], C.blocked_by=[B.id]
```

**对比 Claude Code**：Claude Code 创建线性依赖需要 2 个 LLM turn（先 TaskCreate，再 TaskUpdate addBlockedBy），且依赖 LLM 自觉调 update，不可靠。Twinkle 的 `sequential=True` 1 turn 搞定，不依赖 LLM 记忆。

**对比 jiuwenswarm**：jiuwenswarm 的 `depends` 字段在创建时不填，更多是前端展示用。Twinkle 的 `sequential=True` 让依赖在创建时自动生效。

**复杂依赖**（菱形等）：不在 `todo_create` 中支持。需要时 LLM 调 `todo_update` 设置，这是罕见场景，不优化。

## 6. TodoStore API

### 6.1 增强前

| 方法 | 签名 |
|---|---|
| `create` | `(session_id, tasks: list[str])` |
| `complete` | `(session_id, idx: int, result: str)` |
| `list_tasks` | `(session_id) -> list[TodoTask]` |
| `delete` | `(session_id) -> bool` |

### 6.2 增强后

| 方法 | 签名 | 说明 |
|---|---|---|
| `create` | `(session_id, subjects: list[str], sequential: bool = False) -> list[TodoTask]` | 批量创建，返回创建的任务 |
| `update` | `(session_id, task_id: str, **fields) -> TodoTask` | 通用更新 |
| `list` | `(session_id, status?: str) -> list[TodoTask]` | 可按状态过滤 |
| `get` | `(session_id, task_id: str) -> TodoTask | None` | 按 id 查单个 |
| `delete` | `(session_id) -> bool` | 不变，清空整个 session 的 todo |

### 6.3 `create` 的 `sequential` 实现

```python
async def create(self, session_id, subjects, sequential=False):
    now = time.time()
    tasks = [
        TodoTask(id=str(uuid.uuid4()), subject=s, created_at=now, updated_at=now)
        for s in subjects
    ]
    if sequential:
        for i, t in enumerate(tasks):
            if i > 0:
                t.blocked_by = [tasks[i - 1].id]
    self._save(session_id, tasks)
    return tasks
```

### 6.4 `update` 的守卫逻辑

```python
async def update(self, session_id, task_id, **fields):
    tasks = self._load(session_id)
    task = find_by_id(tasks, task_id)
    # 更新字段...
    if new_status == "in_progress" and task.blocked_by:
        unresolved = [bid for bid in task.blocked_by
                      if find_by_id(tasks, bid).status != "completed"]
        if unresolved:
            # 不拒绝，只附加 warning
            return task, f"Warning: task {task_id} has unresolved dependencies: {unresolved}"
    self._save(session_id, tasks)
    return task, None
```

## 7. Agent Loop 集成

### 7.1 System prompt 更新

替换现有 Todo 段：

```
## Todo（任务规划与追踪）
你有 todo 工具来规划和追踪多步骤任务：todo_create、todo_update、todo_list、todo_get。
- 非平凡的多步骤请求：先调 todo_create 列出子任务，逐步执行并用 todo_update(task_id, status="completed") 标记完成。
- 有顺序依赖的任务：用 todo_create(subjects=[...], sequential=True)，系统自动串联依赖。
- 简单单步请求：直接回答或调工具，不要使用 todo。
```

### 7.2 事件推送

复用现有 `TODO_EVENTS` ContextVar + `flush_todo_events()` 机制，snapshot 结构扩展：

```python
def _snapshot(tasks: list[TodoTask]) -> dict:
    return {
        "tasks": [
            {
                "id": t.id, "subject": t.subject, "description": t.description,
                "status": t.status, "result": t.result,
                "blocked_by": t.blocked_by, "owner": t.owner,
                "metadata": t.metadata,
                "created_at": t.created_at, "updated_at": t.updated_at,
            }
            for t in tasks
        ],
        "remaining": sum(1 for t in tasks if t.status in ("pending", "in_progress")),
        "total": len(tasks),
    }
```

WebSocket 事件类型保持 `e2a.todo_update` / `todo.update`，不改名。前端根据新字段自行扩展渲染。

## 8. 前端变更

### 8.1 TypeScript 类型

```typescript
export interface TodoTask {
  id: string
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
  result: string
  blocked_by: string[]
  owner: string
  metadata: Record<string, unknown>
  created_at: number
  updated_at: number
}
```

### 8.2 TodoPanel 增强

组件名和位置不变，在现有基础上增强：

1. **按状态分组**：in_progress → pending → completed（对齐 jiuwenswarm 的 TodoList）
2. **依赖展示**：`blocked_by` 非空时，在 subject 下方显示小标签 `依赖: xxx`
3. **owner 展示**：`owner` 非空时，显示归属标签
4. **状态图标**：
   - pending：空心圆 `○`
   - in_progress：脉冲圆 `◐`（带动画）
   - completed：实心勾 `✓`
   - cancelled：删除线
5. **key 使用 id**：不再用 idx，用 `id` 前 6 位作为 key

### 8.3 useSessions 更新

`todo` ref 类型同步更新为包含新字段的 `TodoState`。`completedCount` 计算逻辑不变（`status === 'completed'` 的计数）。

## 9. 不做的事

| 项目 | 原因 |
|---|---|
| `todo_create` 不支持 `blocked_by` 参数 | 避免 LLM 需要回头调 update 的不可靠问题；`sequential=True` 覆盖线性场景 |
| `todo_update` 不暴露 `blocked_by` 参数 | LLM 不需要手动设置依赖；`sequential=True` 自动设置；复杂依赖是罕见场景 |
| 不加 `active_form` 字段 | in_progress 时直接显示 subject，前端自动加 spinner |
| 不新增独立 Task 系统 | 一套系统增强到 Task 级别，避免两套并存 |
| 不做向后兼容 | 项目处于开发态，旧格式直接丢弃 |

## 10. jiuwenswarm PlanNode 核心优势记录

> 以下不是本次实现内容，记录 jiuwenswarm 的核心优势，供后续 PPT 生成等场景参考。

### 10.1 核心优势

1. **递归执行树**：PlanNode 支持任意深度的子节点，每个节点有 `instruction` + `sub_plans`，形成树形结构。适合固定流程的复杂编排（如 PPT 生成的 12 步 pipeline）。

2. **结构性执行保证**：`run()` 是模板方法，不允许跳步。`execute_subplan()` 按顺序执行，不会跳过子节点。`fallback_callback` 自动降级，不会默默失败。

3. **中间状态传递**：`inputs` dict 在节点间显式传递，上一个节点的输出直接成为下一个节点的输入。不依赖 LLM 上下文记忆。

4. **安全沙箱**：`PlanCodeValidator` + `_SAFE_BUILTINS` 白名单，限制 import 和内置函数，防止 skill 代码执行危险操作。

5. **LLM 能力注入**：PlanNode 通过回调机制访问 `call_tool`、`call_llm`、`stream_llm`、`extract_json`，节点内部可以调用 LLM 和工具，但受框架约束。

6. **HITL 中断/恢复**：`PermissionInterruptRail` + `AbortError` + `save_resume_ctx()`，支持工具调用时的人类审批中断和恢复执行。

### 10.2 何时需要 PlanNode

当 Twinkle 未来需要以下场景时，应引入 PlanNode 或类似机制：

- **固定流程编排**：如 PPT 生成（12 步 pipeline）、深度研究报告（多步检索+分析+导出）
- **严格中间状态传递**：上一步的输出必须精确传递给下一步，不能靠 LLM 记忆
- **自动 fallback**：节点失败时自动降级重试，不能依赖 LLM 自觉
- **安全约束**：skill 代码需要在沙箱中执行

### 10.3 与本次增强的关系

当前 Todo 增强（id、blocked_by、sequential、owner、metadata）解决的是**LLM 自驱的规划追踪**。PlanNode 解决的是**引擎驱动的流程编排**。两者不冲突，是不同层级的能力：

```
Todo 增强（本次）  → LLM 自驱，灵活，轻量
PlanNode（未来）   → 引擎驱动，可靠，重量
```

## 11. 文件变更清单

| 文件 | 变更 |
|---|---|
| `twinkle/agentserver/todo/store.py` | `TodoTask` 数据模型增强；`TodoStore` API 重写（create/update/list/get） |
| `twinkle/agentserver/todo/context.py` | `_snapshot` 结构扩展 |
| `twinkle/agentserver/todo/__init__.py` | 重导出更新 |
| `twinkle/agentserver/tools/builtin/todo_tools.py` | 4 个工具重写（create/update/list/get） |
| `twinkle/agentserver/agent_loop.py` | system prompt 更新；事件推送适配 |
| `twinkle/e2a/models.py` | 无变更（`e2a.todo_update` 保持） |
| `twinkle/schema/message.py` | 无变更（`todo.update` 保持） |
| `web/src/services/webClient.ts` | `TodoTask` 类型更新 |
| `web/src/composables/useSessions.ts` | `todo` ref 类型更新；`completedCount` 适配 |
| `web/src/components/TodoPanel.vue` | 按状态分组、依赖展示、owner 展示、状态图标更新 |
| `tests/test_todo_store.py` | 重写测试 |
| `tests/test_todo_tools.py` | 重写测试 |
