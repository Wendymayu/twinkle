# 文件操作工具设计（file tools）

- **日期**：2026-07-22
- **状态**：已实现（见 `twinkle/agentserver/tools/builtin/file_tools.py` + `tests/test_file_tools.py`）
- **范围阶段**：Phase 2（工具系统成形）
- **参考实现**：jiuwenswarm 已装的 openjiuwen SDK `harness/tools/filesystem.py`（2032 行，6 个文件工具）

---

## 1. 背景与目标

Twinkle 现有 `builtin/` 工具只有 `web_fetch` / `web_search` / `command_exec` / `todo_tools`，**没有文件操作工具**。本设计新增一组文件工具，参考 jiuwenswarm（openjiuwen SDK）的文件工具集，按 Twinkle 学习型重写项目的定位做取舍。

**目标**：让 agent 能在**工作区内**读、写、精确编辑、列目录、按模式查找文件，且写入/编辑带"先读后写"安全约束。

**非目标（明确不做）**：
- `grep` 内容搜索（先不做；搜索走 `command_exec` 的 `rg`/`findstr`）。
- `delete_file` / `move_file` 独立工具（走 `command_exec` 的 `rm`/`mv`，同 jiuwenswarm 思路）。
- read_file 读图片/PDF/Notebook（要额外依赖 + 多模态模型接线，YAGNI）。
- mtime/size stale-write 校验（方案 b 明确砍）。
- `.agent_history` 操作历史落盘（YAGNI）。
- OS 级硬沙箱（landlock/seccomp/bwrap/cgroup，Linux 专属 + roadmap 推迟）。
- 危险操作审批/审计 rails（roadmap `permissions/` 推迟）。

## 2. 参考实现要点（jiuwenswarm / openjiuwen）

openjiuwen 的 6 个文件工具：`read_file` / `write_file` / `edit_file` / `glob` / `list_files` / `grep`。删/移不单设工具，走 shell `rm`/`mv`。安全模型分三层：工具层（先读后写 + stale + 设备/二进制/size 拦截）→ fs-op 层（固定 `sandbox_root` 列表，独立于 CWD）→ jiuwenbox OS 硬沙箱 + rails 审批。

Twinkle 取其**工具层 + 路径 confinement**，砍 stale 校验、操作历史、OS 沙箱、审批。路径 confinement 不另设 fs-op 层，直接用 `command_exec` 已建立的 `_resolve_workdir` 同款模式（一个模块内 helper），不引入额外分层。

## 3. 范围

**5 个工具**：`read_file` / `write_file` / `edit_file` / `list_files` / `glob`。

| 工具 | 对齐 jiuwenswarm | 备注 |
|---|---|---|
| `read_file` | ✓ `read_file` | 只读文本，砍图片/PDF/Notebook |
| `write_file` | ✓ `write_file` | 全量写；先读后写但不做 stale |
| `edit_file` | ✓ `edit_file` | 精确替换；`old_string` 空→指向 write_file（不兼容建文件） |
| `list_files` | ✓ `list_files` | stdlib `os.scandir` |
| `glob` | ✓ `glob` | stdlib `pathlib.Path.glob`，**无 ripgrep 依赖** |
| ~~`grep`~~ | ✗ | 先不做，走 command_exec |
| ~~delete/move~~ | ✗ | 走 command_exec `rm`/`mv` |

## 4. 方案：自包含模块（方案 A）

新建一个自包含模块 `twinkle/agentserver/tools/builtin/file_tools.py`，内含 5 个 `@tool` async 函数 + 模块内共享 helper。**不动框架层（base/decorator/schema_extractor/manager），不动 command_exec**，只新增 + 在 `tool_manager()` 注册。

替代方案 B（抽 `tools/_path_safety.py` 让 command_exec 与 file_tools 共用 confinement 逻辑）**不采纳**：要改已测在用的 command_exec，属范围外重构，等第 N 个需要 confinement 的工具出现才回本。

## 5. 模块与注册

```
twinkle/agentserver/tools/builtin/file_tools.py     # 新增：5 个 @tool + helper
twinkle/agentserver/tools/__init__.py               # 改：import + tool_manager() 注册 5 个
tests/test_file_tools.py                            # 新增：单测
```

`tools/__init__.py` 改动：
- import 行加 `file_tools`：
  ```python
  from twinkle.agentserver.tools.builtin import command_exec, file_tools, todo_tools, web_fetch, web_search
  ```
- `tool_manager()` 末尾注册 5 个：
  ```python
  tm.register(file_tools.read_file)
  tm.register(file_tools.write_file)
  tm.register(file_tools.edit_file)
  tm.register(file_tools.list_files)
  tm.register(file_tools.glob)
  ```

框架层零改动。`agent_loop` 经 `self._tools.schemas()` / `self._tools.execute(name, args)` 自动接入，无需改 loop。

## 6. 工具契约

### 6.1 read_file
```python
@tool
async def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a text file under the workspace with offset/limit pagination. Records the read so write_file/edit_file can enforce read-before-write. Rejects binary files."""
```
- 解析+confine `file_path`；不存在→`[ERROR]: file not found: <path>`；二进制→`[ERROR]: file is binary or unsupported: <path>`。
- utf-8 读（`errors="replace"`），`content.splitlines(keepends=True)` 保留原始换行，取 `lines[offset:offset+limit]`，`"".join` 返回。**保留原始字节**（不重排、不附行号）是为了让 agent 从 read_file 输出里复制的 `old_string` 能在 edit_file 里精确匹配同一字节。
- **记入读取注册表** `registry.mark_read(sid, str(resolved_path))`。
- 超过 limit：内容 + 尾注 `\n...[truncated, N total lines, use offset to page]`（N = 总行数）。
- 返回：纯文本内容（不附行号，避免污染 edit_file 的 old_string 匹配）。

### 6.2 write_file
```python
@tool
async def write_file(file_path: str, content: str) -> str:
    """Write full content to a file under the workspace. Overwriting an existing file requires a prior read_file in this session; new files can be created directly. Content capped at 5 MiB."""
```
- 解析+confine。
- **先读后写**：文件已存在且 `not registry.has_read(sid, resolved_path)`→`[ERROR]: must read_file before overwriting existing file: <path>`。新文件（不存在）直接写，不要求先读。
- `len(content) > 5*1024*1024`→`[ERROR]: content too large (>5 MiB)`。
- 父目录不存在→`os.makedirs(parent, exist_ok=True)`。
- utf-8 写；写完 `registry.mark_read(sid, resolved_path)`（写完即知，链式 edit 不必重读）。
- 返回 JSON：`{"file_path": <rel>, "bytes_written": N, "type": "create"|"update"}`。

### 6.3 edit_file
```python
@tool
async def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace old_string with new_string in a file under the workspace. Requires a prior read_file in this session. old_string must be non-empty (use write_file for new files). Set replace_all to replace multiple occurrences."""
```
- 解析+confine；不存在→`[ERROR]: file not found: <path>`。
- **先读后写**：`not registry.has_read`→`[ERROR]: must read_file before editing: <path>`。
- 二进制→拒。
- `old_string == ""`→`[ERROR]: old_string is empty; use write_file to create a new file: <path>`。
- count `old_string` 在当前内容中的出现次数：
  - 0→`[ERROR]: old_string not found in <path>`。
  - `>1` 且 `not replace_all`→`[ERROR]: old_string matches N times; set replace_all=True or provide a more specific old_string.`。
  - 否则：`replace_all=True` 用 `content.replace(old_string, new_string)`（替全部），否则用 `content.replace(old_string, new_string, 1)`（替首个——注意 `str.replace` 默认全替，单次必须显式传 `1`）。替换数 N = `content.count(old_string)`（≥1，非重叠计数，与 `str.replace` 语义一致）。
- 写回；`registry.mark_read(sid, resolved_path)`。
- 返回 JSON：`{"file_path": <rel>, "replacements": N}`。

### 6.4 list_files
```python
@tool
async def list_files(path: str = ".", show_hidden: bool = False) -> str:
    """List entries in a directory under the workspace. Set show_hidden to include dotfiles."""
```
- 解析+confine `path`；不存在→`[ERROR]: path not found: <path>`；非目录→`[ERROR]: not a directory: <path>`。
- `os.scandir` 收集 entries；`show_hidden=False` 过滤以 `.` 开头的名字。
- 返回 JSON：`{"path": <rel>, "entries": [{"name": ..., "type": "file"|"dir"|"other"}]}`（`is_dir()`→`"dir"`，`is_file()`→`"file"`，否则`"other"`）。
- 不与读取注册表交互（列目录不等于读文件内容）。

### 6.5 glob
```python
@tool
async def glob(pattern: str, path: str = ".") -> str:
    """Find files under the workspace matching a glob pattern (stdlib pathlib, no ripgrep). path is the base directory; pattern must not contain '..'."""
```
- 解析+confine `path`（base 必须在 `WORKSPACE_DIR` 内）。
- `pattern` 含 `..`→`[ERROR]: pattern must not contain '..': <pattern>`。
- `Path(resolved).glob(pattern)` 收集匹配；每条结果再 `relative_to(root)` 过滤一遍防越界（defense-in-depth）。
- 返回 JSON：`{"pattern": ..., "path": <rel>, "matches": [<rel paths>]}`。

## 7. 安全模型（方案 b）

### 7.1 路径 confinement
`_resolve_file_path(file_path: str) -> Path`：复刻 `command_exec._resolve_workdir` 的文件版。
```python
def _resolve_file_path(file_path: str) -> Path:
    root = Path(WORKSPACE_DIR).resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(root)   # 越界抛 ValueError
    return candidate
```
- 相对路径拼 `WORKSPACE_DIR`；绝对路径接受但须落在 `WORKSPACE_DIR` 内（jiuwenswarm/Claude Code 风格，比 command_exec 的"只收相对 workdir"更灵活）。
- 越界→调用方 catch `ValueError` 返 `[ERROR]: path is outside the project workspace: <path>`。
- 失败原因与 command_exec 的 `workdir outside the project workspace` 文案对齐，便于 agent 理解。

### 7.2 先读后写（FileReadRegistry）
模块级单例，对齐 `TodoStore` 形态：
```python
class FileReadRegistry:
    def __init__(self) -> None:
        self._read: dict[str, set[str]] = {}      # session_id -> {resolved path str}

    def mark_read(self, sid: str, path: str) -> None: ...   # 同步：set.add 幂等
    def has_read(self, sid: str, path: str) -> bool: ...      # 同步：set 成员查询
    def clear(self, sid: str) -> None: ...         # 测试钩子

# 模块级单例 + session 路由 import（与 todo_tools 同款）：
_registry = FileReadRegistry()
from twinkle.agentserver.plan_todo_context import get_plan_todo_session_id
```
- **session 路由**：`sid = get_plan_todo_session_id()`（与 `todo_tools` 同一路由；`ContextVar` 由 `AgentLoop.run_stream` 在请求入口设置）。测试据此 monkeypatch `file_tools.get_plan_todo_session_id` 返回唯一 sid。
- **键用 resolved path 字符串**（`str(resolved_path)`），保证相对/绝对同一路径命中同一键。
- **强制点**：`write_file`（覆盖既有）/ `edit_file` 进来先 `has_read`，未读即拒；两者完成后 `mark_read`，保证"写完即知"、链式 edit 不必重读。新文件 `write_file` 不要求先读（无内容可盲覆盖）。
- **不做 stale 校验**：不记 mtime/size，不检测"读后被改"。
- **不加 asyncio.Lock**（偏离原草拟）：`mark_read`/`has_read` 是单次 `set.add` / `in`，单事件循环上原子（op 内无 `await`，无 TOCTOU）。`TodoStore` 的锁保护 list 读改写逻辑、本注册表是幂等单 op，不需要；且长生命周期的 `asyncio.Lock` 挂模块单例上、跨 `asyncio.run` 测试循环会绑死在一个 loop 报错，砍掉更简。方法同步，工具内直接调用（不 `await`）。

### 7.3 二进制与 size
- `_is_binary(path: Path) -> bool`：扩展名黑名单（`.pyc/.png/.jpg/.jpeg/.gif/.pdf/.zip/.gz/.tar/.exe/.dll/.so/.dylib/.class/.ipynb` 等）+ 首 8KB null-byte 嗅探。
- write `len(content) > 5 MiB` 拒；read 走 offset/limit 分页（默认 limit=2000 行）。

### 7.4 阻塞 IO
read/write/`os.scandir`/`pathlib.glob` 全走 `asyncio.to_thread`（对齐 command_exec 的 `_run_command_sync` 用法），避免阻塞事件循环。

## 8. 返回形态与错误处理

对齐 `command_exec`：
- **错误**：返回 `[ERROR]: <reason>` 字符串（不抛异常；`ToolManager.execute` 也兜底，但工具层自转更可读）。
- **list_files / glob / write_file / edit_file 成功**：返回 JSON 串（`json.dumps(..., ensure_ascii=False)`）。
- **read_file 成功**：返回纯文本内容。
- 非法参数（空 file_path、非预期类型）→ 开头 catch 返 `[ERROR]: ...`，schema_extractor 不保证模型给的类型正确，工具内做防御性 coerce（如 `int(offset)` 失败回退默认，对齐 command_exec 对 `timeout_seconds` 的处理）。

## 9. 数据流（典型读→改链）

```
read_file(a.py)            → 内容 + registry.mark_read(sid, a.py)
edit_file(a.py, old, new)  → has_read ✓ → 替换 → 写回 → registry.mark_read(sid, a.py)
edit_file(a.py, ...)       → has_read ✓（上一步 mark 的）→ 继续
write_file(b.py, content)  → b.py 不存在 → 直接建 → registry.mark_read(sid, b.py)
write_file(a.py, content)  → a.py 存在 + has_read ✓ → 覆盖 → mark_read
write_file(c.py, content)  → c.py 存在 + has_read ✗ → [ERROR]: must read_file ...
```

## 10. 配置

复用 `twinkle.config.WORKSPACE_DIR`（默认 `~/.twinkle` 用户家目录，可 `TWINKLE_WORKSPACE_DIR` 覆盖）。`file_tools.py` 顶部 `from twinkle.config import WORKSPACE_DIR`（与 command_exec 同款 import，便于测试 monkeypatch `file_tools.WORKSPACE_DIR` 重定向到临时目录）。不新增配置项。

## 11. 测试（`tests/test_file_tools.py`）

对齐 `test_command_exec.py`：`asyncio.run()` + monkeypatch 内部 hook，**不依赖 pytest-asyncio**。

- **fixture**：pytest `tmp_path` + `monkeypatch.setattr(file_tools, "WORKSPACE_DIR", str(tmp_path))` 把 confinement 重定向到临时目录，跑真实文件 IO（快且安全）；每测 monkeypatch `file_tools.get_plan_todo_session_id` 返回唯一 sid，并 `file_tools._registry.clear(sid)` 清理。
- **路径**：越界拒（`../`）、绝对路径（在 tmp 内）接受、相对拼接、`_resolve_file_path` 行为。
- **read_file**：读文本、二进制拒、not found、offset/limit 截断+尾注、`mark_read` 命中注册表。
- **write_file**：新文件直建、覆盖须先读否则拒、超大拒、JSON 形态、写后 `mark_read`。
- **edit_file**：须先读、old_string 0 匹配拒、多匹配非 replace_all 拒、replace_all、单次替换、空 old_string 拒、写后 `mark_read`。
- **list_files**：列项+类型、hidden 过滤、非目录拒、越界拒。
- **glob**：匹配、`..` 拒、结果越界过滤、base 越界拒。
- **注册表 session 隔离**：sid A 的 read 不开放 sid B 的 write。
- **并发/阻塞 IO**：`asyncio.to_thread` 包裹的 read/write 可被 `asyncio.run` 正常 await（不单独写并发压力测试，YAGNI）。

## 12. 与 jiuwenswarm 对照

| 维度 | jiuwenswarm / openjiuwen | Twinkle |
|---|---|---|
| 工具集 | 6 个（含 grep） | 5 个（砍 grep） |
| 删/移 | 走 shell rm/mv + 删除历史 | 走 command_exec rm/mv（不记历史） |
| 路径防护 | 独立 fs-op 层（sandbox_root 列表，独立 CWD） | 单 helper `_resolve_file_path`（confine 到 WORKSPACE_DIR） |
| 先读后写 | 强制 + mtime/size stale 校验 | 强制（方案 b），**不做 stale** |
| 操作历史 | `.agent_history/file_ops_*.json` | 不做 |
| 二进制/size | 设备黑名单 + 二进制 + size/token 上限 | 二进制扩展名+null 嗅探 + 5MiB write + offset/limit read |
| OS 沙箱 | landlock+seccomp+bwrap+cgroup | 不做（Windows + roadmap 推迟） |
| 审批 | rails 层（PlanApproval 等） | 不做（roadmap `permissions/` 推迟） |
| 注册 | 装饰器+元数据 provider 类+harness manifest+config 白名单 | `@tool` + 单跳 `ToolManager.register`（Twinkle 既有约定） |

## 13. 实现顺序（给 writing-plans 的提示）

1. `file_tools.py` 骨架：`_resolve_file_path` / `_is_binary` / `FileReadRegistry` 单例 + import。
2. `read_file`（先有它，write/edit 才能测先读后写）。
3. `write_file` + `FileReadRegistry` 强制点。
4. `edit_file`。
5. `list_files` + `glob`。
6. `tools/__init__.py` 注册 5 个。
7. `tests/test_file_tools.py` 全套。
8. 跑 `python -m pytest tests/test_file_tools.py -v` + `tests/test_tool_manager.py`（确认注册）全绿。
