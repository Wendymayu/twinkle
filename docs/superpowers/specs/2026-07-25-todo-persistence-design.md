# Todo 持久化设计

## 一句话概括

把 `TodoStore` 从纯内存 `dict[session_id, list[TodoTask]]` 改成磁盘持久化:per-session 一个 `<TODOS_DIR>/<sid>.json`,每次操作 load→改→save,跨进程重启存活。对齐 jiuwenswarm 自家 `TodoToolkit`(`todo_toolkits.py`)的机制,沿用 Twinkle SessionStore 的 JSON/async 约定。

---

## 背景与动机

当前 `todo/store.py` 的 `TodoStore` 是纯内存,`dict[sid, list[TodoTask]]` + per-session `asyncio.Lock`。进程一重启,todo 全丢。`docs/design/todo-design.md` §存储策略 当初刻意选内存,理由之一是"session 结束就失效、重启后列表自然消失"。

用户要改这个:参考 jiuwenswarm 的 todo 实现,若它用了持久化,Twinkle 也做。已确认(见下方"参考实现")——**jiuwenswarm 确实持久化**,所以 Twinkle 跟进。

---

## 参考实现(jiuwenswarm)

jiuwenswarm 有两套 todo 实现,根源在依赖结构:

| | Store A — openjiuwen `TodoTool` | Store B — `TodoToolkit` ← Twinkle 参照 |
|---|---|---|
| 归属 | `openjiuwen` **外部框架包**(`site-packages/openjiuwen/harness/tools/todo.py`) | jiuwenswarm 仓库内 `jiuwenswarm/agents/harness/common/tools/todo_toolkits.py` |
| 文件 | `{workspace}/{sid}/todo.json` | `{sessions_dir}/{sid}/todo.md` |
| 格式 | JSON | Markdown |
| 锁 | `asyncio.Lock` per 文件路径(`FileLockManager`) | `threading.Lock` per session |
| 数据 | `TodoItem`:id(UUID)/content/activeForm/status/createdAt/updatedAt | `TodoTask`:**idx/tasks/status/result**(与 Twinkle 一模一样) |
| 生命周期 | 每次 op load→改→save,无缓存 | 同上 |
| session 路由 | `_resolve_file_path(session, session_id)` | 构造时传 `session_id`;配套被删的 `plan_todo_context.py` 提供 `PLAN_TODO_SESSION_ID` ContextVar |

Twinkle 刻意不依赖 openjiuwen(roadmap "明确超出范围"),故 **Store A 与 Twinkle 无关**。Twinkle 已在镜像 **Store B**:`TodoTask` 字段、`PLAN_TODO_SESSION_ID`/`get_plan_todo_session_id()` ContextVar 路由、三工具极简(create/complete/list)都一致。**缺口纯粹在存储层**——把内存 dict 换磁盘文件。本次照 Store B 的机制做:per-session 文件、每次 op load→改→save、per-session 锁、跨重启存活。

(Store B 的 `__` 子作用域隔离用于 spawn/fork subagent,属 Phase 8,未落地,本次不做。)

---

## 设计决策

### D1. 数据模型:单条当前列表 / session

同一时刻,一个 session 至多一条 todo 列表(当前进行中的那条)。新一轮规划替换旧的(已完成的)那条。

**为什么不保留多条历史**:每个任务的 `todo_create`/`todo_complete` 调用及其 markdown 结果,本就作为 tool 消息记在 `history.json`(会话对话历史)里,过往规划不会丢。结构化 todo 存储的职责就是"当前进行中的那条",驱动前端 TodoPanel;不需要再在 todo 存储里留历史。

### D2. `create` 语义(持久化唯一的行为改动)

旧:已有列表 → 拒绝(`TodoError("already exists")`)。
新:

- 已有列表**且有未完成任务**(status != "completed")→ 拒绝,防误覆盖进行中的规划。
- 已有列表**但全部完成** → 允许 `create` 替换(覆盖旧列表)。
- 无列表 → 创建。

错误消息从 "already exists" 改为 **"already in progress"**(语义更准:`create` 不再因为"存在"就拒绝,而是因为"进行中"才拒绝)。`create/complete/list` 其余行为不变。

### D3. 存储机制:JSON + 无缓存,独立 `TODOS_DIR` 与 sessions 并列

- **位置**:新增 `TODOS_DIR`,与 `SESSIONS_DIR` 并列(默认 `<WORKSPACE>/.twinkle_data/todos`)。todo 文件 flat 布局:`<TODOS_DIR>/<sid>.json`(一文件一 session)。sid 沿用 SessionStore 已有的"路径安全组件"信任假设(`SessionStore._session_dir` 同样直接 `root / sid`)。
- **格式**:JSON。`dataclasses.asdict(t)` 存,`TodoTask(**rec)` 重建——精确往返,无需 md 解析器,与 SessionStore 一致。(Store B 用 markdown,但 md↔TodoTask 解析多一坨代码 + bug 面,功能无增益,故 Twinkle 选 JSON。)
- **缓存策略**:**无缓存**,每次 op 都 load→(改)→save,对齐 Store B 的 `_load_tasks`/`_save_tasks`。todo 数据极小(典型 3–5 条)、操作稀疏,内存缓存收益微乎其微,反而引入缓存一致性复杂度。
- **锁**:保留 `self._locks: dict[sid, asyncio.Lock]`(per-session),串行化 read-modify-write。去掉 `self._data` 内存 dict。

### D4. 清理:`session.delete` 显式清 todo

co-locate 在 session 目录里时,`SessionStore.delete_session` 的 `shutil.rmtree(sdir)` 顺手就清了 todo。**独立 `TODOS_DIR` 后这条免费清理没了**,必须显式接,否则 `<sid>.json` 在 session 删除后变孤儿文件堆积。

- `TodoStore` 加 `delete(sid) -> bool`:持 per-session 锁 → `unlink`(文件不存在返 False)。持锁是为了避免与并发 `complete` 的 load→save 产生"删了又被重建"竞态。
- `session.delete` RPC(`sessions/handlers.py` 的 `dispatch_session_rpc`)在 `store.delete_session(sid)` 之后追加 `await todo_store.delete(sid)`。
- **RPC 路径与工具路径用同一个 `TodoStore` 实例**(经 `todo/__init__.py` 的 `get_todo_store()` 进程级单例访问器,惰性构造、处处共享),避免两套锁互不认、`delete` 与 `complete` 各持一把锁失去互斥。
- **不**像 SessionStore 那样 DI 穿参:SessionStore 之所以能 DI,是因为 loop 和 `ws_handler` 都由 `server.py` 构造、可显式注入同一实例;而 todo 工具是模块级 `@tool` 函数(签名即 JSON schema),不便接收 DI。故改用进程级单例访问器 `get_todo_store()` 达到同样的"一处构造、处处共享",`server.py`/`__main__.py` 无需改动。

### D5. 错误处理

- **`<sid>.json` 缺失/损坏**:`_load` 返回 `[]` + 日志,**不抛**(对齐 SessionStore "坏行跳过不抛")。→ `create` 当"无列表"正常创建;`list` 返空;`complete` 报 "not found"。损坏记录逐条跳过重建(镜像 SessionStore 的 per-line skip)。
- **磁盘写失败**(满盘/权限):`_save` 捕 `OSError` 包成 `TodoError("failed to persist todo: …")`,让工具层把错误串回给模型,**而不是炸掉 `run_stream`**。这是相对 SessionStore 的一个刻意分歧——todo 是辅助功能,不该因写盘失败拖垮整个 agent run。
- **非原子写**(已知局限):`_save` 用 `Path.write_text` 原地覆盖,非原子;极端的写盘中失败可能留下截断文件,下次 `_load` 当损坏处理返 `[]`。若要严格原子可换 temp-file + `os.replace`,当前不做(YAGNI,非关键功能)。

---

## 改动范围

| 文件 | 改动 |
|---|---|
| `twinkle/agentserver/todo/store.py` | 重写为磁盘持久化 + 加 `delete(sid)`(核心改动) |
| `twinkle/agentserver/todo/__init__.py` | 加 `get_todo_store()` 进程级单例访问器 + `_set_todo_store()` 测试钩子 |
| `twinkle/agentserver/tools/builtin/todo_tools.py` | 每个 @tool 函数改 call-time `store = get_todo_store()`(不再模块级捕获,使测试 swap 即时生效) |
| `twinkle/config.py` | 新增 `TODOS_DIR`(env `TWINKLE_TODOS_DIR`) |
| `twinkle/agentserver/sessions/handlers.py` | `session.delete` 分支追加 `await get_todo_store().delete(sid)`(**不改** `dispatch_session_rpc` 签名) |
| `twinkle/agentserver/server.py` | **不动**(单例经访问器取,无需穿参) |
| `twinkle/agentserver/__main__.py` | **不动**(同上) |
| `twinkle/agentserver/agent_loop.py` | **不动**(todo 路由/事件接线零变更) |
| `twinkle/agentserver/sessions/store.py` | **不动** |
| `tests/test_todo_store.py` | 构造改 `TodoStore(tmp_path)`;改 `create_twice` 的 match;新增持久化/替换/损坏/删除用例 |
| `tests/test_todo_tools.py` | 单例注入 `TodoStore(tmp_path)`(monkeypatch 模块属性) |
| `tests/test_*.py`(涉及 `ws_handler`/`dispatch_session_rpc` 旧签名) | 跟随新签名补 `todo_store` 参数 |
| `docs/design/todo-design.md` | §存储策略 / §与 jiuwenclaw 的差异:内存→磁盘;补 `create` 新语义 + `TODOS_DIR` |
| `CLAUDE.md` | `todo_store.py` 条目:"in-memory…No persistence" → "磁盘持久化(`<sid>.json`)";config 表加 `TWINKLE_TODOS_DIR` 行 |
| `docs/architecture.md` | 若 §4.x 描述 todo 存储,一并核对更新 |

---

## 组件详解:`TodoStore` 磁盘化

### 构造与路径

```python
class TodoStore:
    def __init__(self, todos_dir: str | Path) -> None:
        self._root = Path(todos_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _todo_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())
```

### I/O(对齐 Store B `_load_tasks`/`_save_tasks`,无缓存)

```python
def _load(self, session_id: str) -> list[TodoTask]:
    p = self._todo_path(session_id)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("skipping corrupt todo file %s: %s", session_id, exc)
        return []
    out: list[TodoTask] = []
    for rec in data:                      # 逐条重建,坏记录跳过(镜像 SessionStore per-line skip)
        t = self._record_to_task(rec)
        if t is not None:
            out.append(t)
    return out

def _save(self, session_id: str, tasks: list[TodoTask]) -> None:
    try:
        self._root.mkdir(parents=True, exist_ok=True)
        self._todo_path(session_id).write_text(
            json.dumps([dataclasses.asdict(t) for t in tasks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise TodoError(f"failed to persist todo: {exc}") from exc

@staticmethod
def _record_to_task(rec: dict) -> TodoTask | None:
    try:
        return TodoTask(
            idx=int(rec["idx"]), title=str(rec["title"]),
            status=str(rec["status"]), result=str(rec.get("result", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None
```

### 方法

```python
async def create(self, session_id: str, tasks: list[str]) -> None:
    if not tasks:
        raise TodoError("tasks must be a non-empty list.")
    async with self._lock(session_id):
        existing = self._load(session_id)
        if existing and any(t.status != "completed" for t in existing):
            raise TodoError(f"todo list already in progress for session {session_id}.")
        new = [TodoTask(idx=i + 1, title=t, status="waiting", result="")
               for i, t in enumerate(tasks)]
        self._save(session_id, new)

async def complete(self, session_id: str, idx: int, result: str = "") -> None:
    async with self._lock(session_id):
        tasks = self._load(session_id)
        for t in tasks:
            if t.idx == idx:
                if t.status == "completed":
                    raise TodoError(f"Task {idx} is already completed.")
                t.status = "completed"
                t.result = (result or "").strip() or "done"
                self._save(session_id, tasks)
                return
        raise TodoError(f"Task {idx} not found.")

async def list_tasks(self, session_id: str) -> list[TodoTask]:
    async with self._lock(session_id):
        return list(self._load(session_id))

async def delete(self, session_id: str) -> bool:
    async with self._lock(session_id):
        p = self._todo_path(session_id)
        if not p.is_file():
            return False
        try:
            p.unlink()
        except OSError as exc:
            log.warning("todo delete failed for %s: %s", session_id, exc)
            return False
        return True
```

`TodoTask` dataclass 字段不变(`idx/title/status/result`),`TodoError` 不变。

### `get_todo_store()` 单例访问器

```python
# twinkle/agentserver/todo/__init__.py
_TODO_STORE: TodoStore | None = None


def get_todo_store() -> TodoStore:
    """进程级单例 TodoStore(惰性构造,处处共享同一实例 + 同一套锁)。

    不像 sessions/__init__.py 的 session_store() 返 fresh 实例(DI 穿参用)——
    todo 工具是模块级 @tool 函数,不便接收 DI,故用单例访问器达到"一处构造、
    处处共享"。lazy import config 避免 import-time 副作用。
    """
    global _TODO_STORE
    if _TODO_STORE is None:
        from twinkle.config import TODOS_DIR
        _TODO_STORE = TodoStore(TODOS_DIR)
    return _TODO_STORE


def _set_todo_store(store: "TodoStore | None") -> None:
    """测试钩子:替换/重置单例(配 tmp_path 盘)。生产代码不调。"""
    global _TODO_STORE
    _TODO_STORE = store
```

todo_tools.py 的每个 @tool 函数与 handlers.py 的 `session.delete` 分支都 call-time 调 `get_todo_store()`,所以测试 `_set_todo_store(TodoStore(tmp_path))` 后两条路径即时生效。

---

## 数据流(不变,仅 store 内部多一次磁盘)

```
todo_create 工具
  → get_plan_todo_session_id()            # ContextVar,不变
  → TodoStore.create(sid, tasks)
      → _lock(sid) → _load(sid) → 校验 → _save(sid, new)   # 新增磁盘 I/O
  → list_tasks(sid) → _load(sid)          # 重读拼 markdown + snapshot
  → append_todo_event(snapshot)           # 不变
  → return markdown
```

`complete` 同理;`list` 纯 `_load`。`agent_loop` 的 `reset_todo_events`/`flush_todo_events`/`e2a.todo_update` 全不动。

**清理流**:

```
session.delete RPC
  → SessionStore.delete_session(sid)      # 删 <SESSIONS_DIR>/<sid>/ (不动)
  → get_todo_store().delete(sid)          # 删 <TODOS_DIR>/<sid>.json (新,同实例同锁)
```

---

## 配置

`twinkle/config.py` 紧跟 `SESSIONS_DIR` 加:

```python
# --- Todos persistence (disk-backed per-session todo store) ---
# Flat layout: <TODOS_DIR>/<session_id>.json (one file per session). Defaults
# to <WORKSPACE_DIR>/.twinkle_data/todos — parallel to sessions/, NOT co-located
# inside the session dir, so session deletion must explicitly clean up todos
# (TodoStore.delete wired into the session.delete RPC). Override with
# TWINKLE_TODOS_DIR (~/... expanded).
TODOS_DIR = os.getenv("TWINKLE_TODOS_DIR") or str(
    Path(WORKSPACE_DIR) / ".twinkle_data" / "todos"
)
```

无新 RPC、无 `list_todos`(YAGNI——todo 永远经 session_id 走工具访问)。

---

## 测试策略

沿用 repo 约定:`asyncio.run()` + pytest `tmp_path`,**不用 pytest-asyncio**。

### `tests/test_todo_store.py`

既有 8 个用例改构造为 `TodoStore(tmp_path)`;`test_create_twice_raises_already_exists` 的 `match` 从 "already exists" 改 "in progress";`test_sessions_isolated` / `test_concurrent_complete_no_lost_update` / `test_complete_*` 行为不变应直接通过。

**新增**:

- `test_persistence_across_restart`:用 store A 在 `tmp_path` create,再用 store B(同 `tmp_path`)`list_tasks` → 见到落盘的列表。**本特性的核心验收**。
- `test_create_replaces_when_all_completed`:create→全 complete→再 create 成功且为新列表。
- `test_create_refuses_while_in_progress`:create→再 create(未完成)→ 抛 "in progress"。
- `test_load_corrupt_json_returns_empty`:写垃圾进 `<sid>.json`,`list_tasks` → `[]`,`create` → 成功(当无列表)。
- `test_delete_removes_file`:`delete` 后文件不在,`list_tasks` → `[]`。
- `test_delete_missing_returns_false`:无文件时 `delete` 返 False 不抛。

### `tests/test_todo_tools.py`

setup 调 `_set_todo_store(TodoStore(tmp_path))`(配 `tmp_path` 临时盘,避免写到真实 `TODOS_DIR`);teardown `_set_todo_store(None)` 复位。因 todo_tools 与 handlers 都 call-time 调 `get_todo_store()`,swap 后两条路径即时生效。具体结构写 plan 时照现状对齐。

### `tests/test_*.py`(无需签名改)

`ws_handler(loop, store)` / `dispatch_session_rpc(envelope, store)` 签名**不变**(单例经访问器取,未穿参),`test_permissions_e2e`、`test_approval_flow` 等无需补参。仅当某测试断言 todo 行为时,setup 调 `_set_todo_store(...)` 注入临时盘。

---

## 文档同步

- `docs/design/todo-design.md`:
  - §存储策略:从"纯内存,不持久化"改写为"磁盘持久化,per-session `<TODOS_DIR>/<sid>.json`,load/save per op",并说明 `create` 新语义(in-progress 拒绝、all-completed 替换)。
  - §与 jiuwenclaw 的差异 表:Twinkle 存储格从"纯内存 dict"改为"磁盘 JSON";补"独立 `TODOS_DIR`,session.delete 显式清理"。
- `CLAUDE.md`:
  - `todo_store.py` 条目:"in-memory `TodoStore`(`dict[...]`)…No persistence" → "磁盘持久化 `TodoStore`(`<TODOS_DIR>/<sid>.json`),per-session `asyncio.Lock`,load/save per op,跨重启存活;`delete(sid)` 由 `session.delete` RPC 清理"。
  - 配置表加 `TWINKLE_TODOS_DIR` 行。
- `docs/architecture.md`:§4.x 若描述 todo 存储,一并核对更新。

---

## 非目标(YAGNI)

- **不**保留多条 todo 历史(D1:过往规划已在 `history.json`)。
- **不**加 `todo_reset` 工具(`create` 新语义已覆盖替换需求)。
- **不**加 `list_todos` RPC / 不暴露 todo 文件浏览(todo 经 session_id 走工具访问)。
- **不**做内存缓存(D3:收益微小、引入一致性复杂度)。
- **不**做 markdown 格式(D3:JSON 精确往返、无解析器)。
- **不**做 subagent 的 `__` 子作用域隔离(Phase 8,未落地)。
- **不**改 `agent_loop.py` 的 todo 接线(路由/事件零变更)。

---

## 验收

1. 进程重启后,某 session 的 todo 列表仍在(`test_persistence_across_restart`)。
2. 同一 session 内:任务全完成后可再 `create` 新规划;进行中再 `create` 被拒(`test_create_replaces_when_all_completed` + `test_create_refuses_while_in_progress`)。
3. `session.delete` 后 `<TODOS_DIR>/<sid>.json` 不留孤儿(`test_delete_removes_file`)。
4. `<sid>.json` 损坏不抛、当空列表处理(`test_load_corrupt_json_returns_empty`)。
5. `create`/`complete`/`list` 三个工具行为(除 `create` 的替换语义)与现状一致;并发无丢更新(`test_concurrent_complete_no_lost_update` 仍过)。
