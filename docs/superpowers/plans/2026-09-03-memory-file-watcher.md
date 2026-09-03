# Memory 文件监听器（watchdog 反向触发）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MemoryManager 的 sqlite 索引由 watchdog 文件事件反向触发（对齐 jiuwenswarm/openclaw 模型 B），write 只落盘不再主动 `_mark_dirty`，补外部编辑 + 删文件幽灵 chunk 两个 gap。

**Architecture:** watchdog Observer 监听 `memory_dir` 递归，事件经 `_validate_memory_path` 白名单过滤后调现有 `_mark_dirty`（复用 set + 2s Timer 防抖）或新增 `_remove_file_from_index`。write/edit/replace 删除进程内 `_mark_dirty` 调用，dirty 完全由 watchdog 标。保留 `set` 精确路径 + search if-dirty 兜底 + interval 默认关可配置开。

**Tech Stack:** Python 3.11+、`watchdog>=4.0`、sqlite3（现有）、threading.Timer（现有防抖）、pytest（asyncio.run 模式，无 pytest-asyncio）。

**关联 spec:** `docs/superpowers/specs/2026-09-03-memory-file-watcher-design.md`

**用户规则:** commit 前先问用户，不自动 commit（见 [[no-direct-github-push]]）。计划内 commit 步骤的命令保留，执行时须先获用户批准再 run。

---

## 文件结构

| 文件 | 责任 | 改动 |
|---|---|---|
| `pyproject.toml` | 依赖 | 主 dependencies 加 `watchdog>=4.0`（模型 B 下索引触发核心，必需非 optional） |
| `twinkle/agentserver/memory/store.py` | MemoryManager | 加 `enable_watcher` kwarg + Observer 启停 + `_MemoryEventHandler` + `_remove_file_from_index` + `close()` + `_ensure_interval_sync`；删 write/edit/replace 三处 `_mark_dirty` |
| `twinkle/agentserver/memory/__init__.py` | 单例 | `get_memory_manager` 默认 watcher + atexit；`_set_memory_manager` close old |
| `twinkle/config/schema.py` | 配置 | `MemoryIndexConfig` 加 `watch_interval_seconds` |
| `twinkle/config/__init__.py` | 配置导出 | 加 `MEMORY_WATCH_INTERVAL_SECONDS` |
| `twinkle/resources/config.yaml` | 配置默认 | memory.index 段加注释 |
| `tests/test_memory_store.py` | 测试 | `_mgr` helper 默认 `enable_watcher=False`；审计改测试触发方式；加 watcher 专项测试 |

**递进原则**：Task 1-6 **保留** write 的 `_mark_dirty`（双触发幂等无害），先让 watcher 可用并验证；Task 7 最后删 write 的 `_mark_dirty`——此时 watcher 已是可靠替代触发源，删了只是去冗余，不破坏。

---

## Task 1: 加 watchdog 依赖 + import 验证

**Files:**
- Modify: `pyproject.toml:10-18`

- [ ] **Step 1: 加依赖**

`pyproject.toml` 的 `[project] dependencies` 列表末尾加一行：

```toml
dependencies = [
    "websockets>=14",
    "pydantic>=2.11",
    "openai>=1.50",
    "httpx>=0.27",
    "pyyaml>=6",
    "croniter>=2",
    "tzdata>=2024.1",
    "watchdog>=4.0",
]
```

放主 dependencies（非 `memory` extras）：模型 B 下 watcher 是索引触发的唯一自动来源，没它 write 后不索引（除非 interval 开）。sqlite-vec/jieba 保持 optional（向量/分词是增强，FTS 兜底）。

- [ ] **Step 2: 安装 + 验证 import**

Run: `pip install watchdog && python -c "from watchdog.observers import Observer; from watchdog.events import FileSystemEventHandler; print('ok')"`
Expected: 输出 `ok`，无异常。

- [ ] **Step 3: 跑现有测试确认无破坏**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS（本 task 只加依赖，不改代码）。

- [ ] **Step 4: Commit（先问用户）**

```bash
git add pyproject.toml
git commit -m "build: add watchdog dependency for memory file watcher"
```

⚠️ 按用户规则，commit 前先问用户批准。

---

## Task 2: `_remove_file_from_index` 方法（TDD，纯 DB）

**Files:**
- Modify: `twinkle/agentserver/memory/store.py`（在 `_index_file` 后新增方法）
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: 写失败测试**

`tests/test_memory_store.py` 末尾加：

```python
def test_remove_file_from_index_clears_chunks(tmp_path):
    """on_deleted 的底层方法:删该文件在索引的全部痕迹(chunks/fts/vec/files),
    不删 embedding_cache(md5 跨 chunk 共享)。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    mgr._flush_now()  # noqa: SLF001
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] >= 1
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM files WHERE path='MEMORY.md'").fetchone()[0] == 1
    mgr._remove_file_from_index("MEMORY.md")  # noqa: SLF001
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM files WHERE path='MEMORY.md'").fetchone()[0] == 0
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_memory_store.py::test_remove_file_from_index_clears_chunks -v`
Expected: FAIL with `AttributeError: 'MemoryManager' object has no attribute '_remove_file_from_index'`

- [ ] **Step 3: 实现**

`store.py` 在 `_index_file` 方法之后（约 328 行后）加：

```python
    def _remove_file_from_index(self, relative_path: str) -> None:
        """删该文件在索引里的全部痕迹(chunks/fts/vec/files),供 on_deleted 调。
        不删 embedding_cache(其 key=md5(text) 跨 chunk 共享,删了影响别处复用)。
        对齐 _index_file 删旧 chunk 的 rowid 模式(store.py:283-290)。"""
        with self._db_lock:
            stale_row_ids = [r["rowid"] for r in self._db.execute(
                "SELECT rowid FROM chunks WHERE path=?", (relative_path,)).fetchall()]
            if stale_row_ids:
                placeholders = ",".join("?" * len(stale_row_ids))
                self._db.execute("DELETE FROM chunks WHERE path=?", (relative_path,))
                self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})",
                                 stale_row_ids)
                if self._vec_enabled:
                    self._db.execute(
                        f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                        stale_row_ids)
            self._db.execute("DELETE FROM files WHERE path=?", (relative_path,))
            self._db.commit()
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_memory_store.py::test_remove_file_from_index_clears_chunks -v`
Expected: PASS

- [ ] **Step 5: Commit（先问用户）**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "feat(memory): add _remove_file_from_index for on_deleted cleanup"
```

---

## Task 3: `enable_watcher` kwarg + Observer 启停 + `_MemoryEventHandler` + `close()`

**Files:**
- Modify: `twinkle/agentserver/memory/store.py`（`__init__`、新增 `_MemoryEventHandler` 类、`close()`）
- Modify: `tests/test_memory_store.py`（`_mgr` helper 默认 `enable_watcher=False`）

- [ ] **Step 1: 改 `_mgr` helper 默认不起 observer**

`tests/test_memory_store.py:6-7`：

```python
def _mgr(tmp_path, **kw):
    kw.setdefault("enable_watcher", False)
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)
```

现有 ~30 个测试调 `_mgr()` → 默认 `enable_watcher=False` 不 spawn observer 线程（防泄露 + 防慢）。watcher 专项测试显式传 `enable_watcher=True`。

- [ ] **Step 2: 写失败测试**

`tests/test_memory_store.py` 末尾加：

```python
def test_watcher_indexes_external_write(tmp_path):
    """外部直接写 MEMORY.md(不经 mgr.write)→ watchdog on_modified → mark_dirty
    → search 兜底搜到。验证 watcher 自动触发外部编辑索引。"""
    import time
    mgr = MemoryManager(str(tmp_path), embed_provider=None,
                        enable_watcher=True, index_debounce_seconds=0.05)
    try:
        (tmp_path / "MEMORY.md").write_text("外部写入的事实。", encoding="utf-8")
        # 等 watchdog 事件 fire(毫秒级)+ 标 dirty
        for _ in range(40):
            if mgr._dirty_paths:  # noqa: SLF001
                break
            time.sleep(0.05)
        assert mgr._dirty_paths, "watchdog 应捕获外部写并标 dirty"  # noqa: SLF001
        hits = mgr.search("外部")
        assert any("外部" in h["text"] for h in hits), "外部写应被索引并召回"
    finally:
        mgr.close()


def test_enable_watcher_false_no_observer(tmp_path):
    """enable_watcher=False 不起 observer(测试隔离)。"""
    mgr = _mgr(tmp_path)  # helper 默认 enable_watcher=False
    assert mgr._observer is None  # noqa: SLF001
    mgr.close()


def test_watcher_degrades_when_import_fails(tmp_path, monkeypatch):
    """watchdog import 失败 → log.warning + 不崩(enable_watcher=True 但降级无 watcher)。"""
    import builtins
    real_import = builtins.__import__

    def _block_watchdog(name, *args, **kwargs):
        if name == "watchdog" or name.startswith("watchdog."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_watchdog)
    mgr = MemoryManager(str(tmp_path), embed_provider=None, enable_watcher=True)
    assert mgr._observer is None  # noqa: SLF001 — 降级无 watcher
    mgr.close()


def test_close_stops_observer(tmp_path):
    """close() 后 observer 线程结束、_observer 置 None(幂等可重入)。"""
    mgr = MemoryManager(str(tmp_path), embed_provider=None, enable_watcher=True)
    assert mgr._observer is not None  # noqa: SLF001
    mgr.close()
    assert mgr._observer is None  # noqa: SLF001
    mgr.close()  # 幂等:二次 close 不抛
```

- [ ] **Step 3: 验证失败**

Run: `python -m pytest tests/test_memory_store.py::test_watcher_indexes_external_write tests/test_memory_store.py::test_enable_watcher_false_no_observer tests/test_memory_store.py::test_watcher_degrades_when_import_fails tests/test_memory_store.py::test_close_stops_observer -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'enable_watcher'`

- [ ] **Step 4: 实现 `__init__` 加 kwarg + Observer 启停**

`store.py` `__init__` 签名（38-52 行）加 `enable_watcher: bool = True`，并在 `_clear_if_model_changed()` 后加 Observer 启停。在文件顶部 import 区（`from pathlib import Path` 附近）不加 watchdog（懒 import 在 `__init__` 内，对齐 sqlite-vec 的 try/except 降级模式）。`__init__` 末尾（79 行 `self._clear_if_model_changed()` 之后）加：

```python
        self._observer = None
        self._interval_timer: threading.Timer | None = None
        if enable_watcher:
            self._start_watcher()

    def _start_watcher(self) -> None:
        """起 watchdog Observer 监听 memory_dir 递归。失败降级无 watcher
        (对齐 _ensure_schema sqlite-vec 可选降级)。事件回调只调 _mark_dirty/
        _remove_file_from_index,复用现有防抖链路。"""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.warning("watchdog unavailable; memory degrades to no-watcher "
                        "(external edits won't auto-index)")
            return
        handler = _MemoryEventHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._dir), recursive=True)
        try:
            self._observer.start()
        except Exception as exc:
            log.warning("watchdog observer start failed: %s; no-watcher", exc)
            self._observer = None
```

`__init__` 签名改成：

```python
    def __init__(
        self,
        memory_dir: str,
        embed_provider=None,
        *,
        dims: int = 1536,
        chunk_tokens: int = 256,
        chunk_overlap: int = 32,
        max_results: int = 10,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
        candidate_multiplier: float = 2.0,
        max_chunks_per_file: int = 200,
        index_debounce_seconds: float = 2.0,
        enable_watcher: bool = True,
    ) -> None:
```

- [ ] **Step 5: 实现 `_MemoryEventHandler` 类 + `close()`**

`store.py` 在 `MemoryManager` 类**之前**（`class MemoryManager:` 之前，模块级）加事件处理类：

```python
class _MemoryEventHandler(FileSystemEventHandler):
    """watchdog 事件 → MemoryManager 的 _mark_dirty / _remove_file_from_index。
    事件回调跑在 watchdog emitter 线程,只做轻量标 dirty(复用 _mark_dirty 的
    set+Timer 防抖),不直接 _index_file(避免每条 OS 噪音事件都重索引+embed)。"""

    def __init__(self, mgr: "MemoryManager") -> None:
        self._mgr = mgr

    def _on(self, abs_path: str, is_delete: bool = False) -> None:
        try:
            # watchdog 给绝对路径 → 转相对;_dir 已 resolve(),这里也 resolve 对齐
            rel = str(Path(abs_path).resolve().relative_to(self._mgr._dir))
        except (ValueError, OSError):
            return  # 不在 memory_dir 下(如系统 temp 目录)→ 忽略
        rel = self._mgr._validate_memory_path(rel)  # 白名单:USER/MEMORY/daily
        if rel is None:
            return
        try:
            if is_delete:
                self._mgr._remove_file_from_index(rel)
            else:
                self._mgr._mark_dirty(rel)
        except Exception:
            log.exception("watcher event handling failed path=%s", rel)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._on(event.src_path)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._on(event.src_path)

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._on(event.src_path, is_delete=True)

    def on_moved(self, event) -> None:
        # replace 的 tmp.replace(fpath) 原子 rename:src=.tmp(白名单外忽略),
        # dest=目标 .md(标 dirty)。daily 改名则 src 走删、dest 走建。
        if not event.is_directory:
            self._on(event.src_path, is_delete=True)
            self._on(event.dest_path)
```

`store.py` 顶部 import 区加（`from watchdog.events import FileSystemEventHandler` 要在类用前，但为降级安全，改在类定义前 try import；更简单：顶部直接 `from watchdog.events import FileSystemEventHandler`——但若 watchdog 没装会让整个 store 模块 import 失败。所以用懒模式：模块顶部不 import watchdog，`_MemoryEventHandler` 继承的基类在 `_start_watcher` 内动态获取太绕。

**改用**：模块顶部 try import，失败则定义一个空基类占位：

`store.py` 顶部 import 区（`from pathlib import Path` 之后）加：

```python
try:
    from watchdog.events import FileSystemEventHandler
except ImportError:  # watchdog 未装 → 降级,基类用空占位
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass
```

这样 `_MemoryEventHandler(FileSystemEventHandler)` 在 watchdog 没装时也能定义（降级时 `_start_watcher` 因 import 失败不实例化 handler，类定义无害）。

`close()` 方法加在 MemoryManager 内（`_start_watcher` 后）：

```python
    def close(self) -> None:
        """停 Observer + 取消 timer + 最终 flush。幂等可重入。
        生产由 atexit + _set_memory_manager 调;测试 try/finally 调。"""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        if self._interval_timer is not None:
            self._interval_timer.cancel()
            self._interval_timer = None
        # 取消 pending sync timer + 最终 flush 剩 dirty
        with self._dirty_lock:
            if self._sync_timer:
                self._sync_timer.cancel()
                self._sync_timer = None
        self._flush_dirty()
```

- [ ] **Step 6: 验证通过**

Run: `python -m pytest tests/test_memory_store.py::test_watcher_indexes_external_write tests/test_memory_store.py::test_enable_watcher_false_no_observer tests/test_memory_store.py::test_watcher_degrades_when_import_fails tests/test_memory_store.py::test_close_stops_observer -v`
Expected: 4 个 PASS

- [ ] **Step 7: 跑全测试确认 helper 改动不破坏现有**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS（helper `enable_watcher=False`，现有测试 write 仍 `_mark_dirty`，不依赖 watcher）

- [ ] **Step 8: Commit（先问用户）**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "feat(memory): add watchdog observer + _MemoryEventHandler + close()"
```

---

## Task 4: `on_moved`（replace rename）+ `on_deleted` 专项测试

**Files:**
- Test: `tests/test_memory_store.py`（handler 已在 Task 3 实现，本 task 验证 replace/删路径）

- [ ] **Step 1: 写测试**

`tests/test_memory_store.py` 末尾加：

```python
def test_watcher_replace_rename_via_on_moved(tmp_path):
    """replace 用 tmp.replace(fpath) 原子 rename → on_moved(dest=目标 .md)
    → 标 dirty → 重建索引。删 write 的 mark_dirty 后这条是 replace 的唯一
    触发路径,必须覆盖。"""
    import time
    mgr = MemoryManager(str(tmp_path), embed_provider=None,
                        enable_watcher=True, index_debounce_seconds=0.05)
    try:
        mgr.write("MEMORY.md", "旧内容 Windows", append=True)
        mgr._flush_now()  # noqa: SLF001 — 先建初始索引
        # replace 原子 rename(内部 tmp.replace(fpath))
        mgr.replace("MEMORY.md", "新内容 macOS")
        # 等 on_moved 标 dirty
        for _ in range(40):
            if mgr._dirty_paths:  # noqa: SLF001
                break
            time.sleep(0.05)
        assert mgr._dirty_paths, "on_moved 应捕获 replace rename 并标 dirty"  # noqa: SLF001
        hits = mgr.search("macOS")
        assert any("macOS" in h["text"] for h in hits), "新内容应被索引"
        assert not any("Windows" in h["text"] for h in hits), "旧内容应已清"
    finally:
        mgr.close()


def test_watcher_on_deleted_clears_index(tmp_path):
    """外部删 .md → on_deleted → _remove_file_from_index → chunks 清。"""
    import time
    mgr = MemoryManager(str(tmp_path), embed_provider=None,
                        enable_watcher=True, index_debounce_seconds=0.05)
    try:
        mgr.write("MEMORY.md", "待删的事实", append=True)
        mgr._flush_now()  # noqa: SLF001
        assert mgr._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] >= 1
        (tmp_path / "MEMORY.md").unlink()  # 外部删
        # 等 on_deleted
        for _ in range(40):
            if mgr._db.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0:
                break
            time.sleep(0.05)
        assert mgr._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    finally:
        mgr.close()
```

- [ ] **Step 2: 验证通过**

Run: `python -m pytest tests/test_memory_store.py::test_watcher_replace_rename_via_on_moved tests/test_memory_store.py::test_watcher_on_deleted_clears_index -v`
Expected: 2 个 PASS（handler 在 Task 3 已实现 on_moved/on_deleted）

- [ ] **Step 3: Commit（先问用户）**

```bash
git add tests/test_memory_store.py
git commit -m "test(memory): cover on_moved (replace rename) + on_deleted paths"
```

---

## Task 5: 生命周期接线（`__init__.py` atexit + `_set_memory_manager` close old）

**Files:**
- Modify: `twinkle/agentserver/memory/__init__.py`
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: 写失败测试**

`tests/test_memory_store.py` 末尾加：

```python
def test_set_memory_manager_closes_old(tmp_path):
    """_set_memory_manager(new) 替换单例时,若 old 存在先 close()(停其 observer)。"""
    import twinkle.agentserver.memory as m
    from twinkle.agentserver.memory import _set_memory_manager, MemoryManager
    old = MemoryManager(str(tmp_path), embed_provider=None, enable_watcher=True)
    assert old._observer is not None  # noqa: SLF001
    _set_memory_manager(old)
    new = MemoryManager(str(tmp_path), embed_provider=None, enable_watcher=True)
    _set_memory_manager(new)
    assert old._observer is None  # noqa: SLF001 — old 被 close 了
    assert m._MEMORY_MANAGER is new  # noqa: SLF001
    _set_memory_manager(None)
    new.close()
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_memory_store.py::test_set_memory_manager_closes_old -v`
Expected: FAIL with `AssertionError: assert old._observer is not None`（`_set_memory_manager` 没调 close）

- [ ] **Step 3: 改 `__init__.py`**

`twinkle/agentserver/memory/__init__.py`：

`get_memory_manager`（32-42 行）构造后加 atexit：

```python
        _MEMORY_MANAGER = MemoryManager(
            MEMORY_DIR, provider, dims=dims,
            chunk_tokens=MEMORY_CHUNKING_TOKENS,
            chunk_overlap=MEMORY_CHUNKING_OVERLAP,
            max_results=MEMORY_QUERY_MAX_RESULTS,
            vector_weight=MEMORY_HYBRID_VECTOR_WEIGHT,
            text_weight=MEMORY_HYBRID_TEXT_WEIGHT,
            candidate_multiplier=MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            max_chunks_per_file=MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE,
            index_debounce_seconds=MEMORY_INDEX_DEBOUNCE_SECONDS,
            enable_watcher=True)
        import atexit
        atexit.register(_MEMORY_MANAGER.close)
    return _MEMORY_MANAGER
```

`_set_memory_manager`（45-48 行）加 close old：

```python
def _set_memory_manager(mgr: MemoryManager | None) -> None:
    """测试钩子:替换/重置单例。生产代码从不调用此函数。
    替换时先 close 旧实例(停其 watchdog observer),防线程泄露。"""
    global _MEMORY_MANAGER
    old = _MEMORY_MANAGER
    if old is not None and old is not mgr:
        old.close()
    _MEMORY_MANAGER = mgr
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_memory_store.py::test_set_memory_manager_closes_old -v`
Expected: PASS

- [ ] **Step 5: 跑全测试**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS

- [ ] **Step 6: Commit（先问用户）**

```bash
git add twinkle/agentserver/memory/__init__.py tests/test_memory_store.py
git commit -m "feat(memory): wire atexit + close-old in _set_memory_manager"
```

---

## Task 6: interval 兜底（默认关，可配置开）+ config

**Files:**
- Modify: `twinkle/config/schema.py:125-126`（`MemoryIndexConfig`）
- Modify: `twinkle/config/__init__.py:51`
- Modify: `twinkle/resources/config.yaml`
- Modify: `twinkle/agentserver/memory/store.py`（`_start_watcher` 后加 `_ensure_interval_sync` + `__init__` 调用）
- Modify: `twinkle/agentserver/memory/__init__.py`（`get_memory_manager` 传 `watch_interval_seconds`）
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: 写失败测试**

`tests/test_memory_store.py` 末尾加：

```python
def test_interval_sync_reindexes_missed_change(tmp_path):
    """watch_interval_seconds>0 → interval 定时全扫,兜 watchdog 漏标。
    模拟漏标:直接落盘(不经 watcher 标 dirty)+ 等 interval 触发 → 索引出现。"""
    import time
    mgr = MemoryManager(str(tmp_path), embed_provider=None,
                        enable_watcher=False,  # 关 watcher 模拟"漏标"
                        index_debounce_seconds=0.05,
                        watch_interval_seconds=0.1)
    try:
        # 直接落盘,不经 mgr.write 也不经 watcher → dirty 空
        (tmp_path / "MEMORY.md").write_text("interval 兜底的事实。", encoding="utf-8")
        # 等 interval(0.1s)触发全扫
        for _ in range(40):
            hits = mgr.search("interval")
            if any("interval" in h["text"] for h in hits):
                break
            time.sleep(0.05)
        assert any("interval" in h["text"] for h in hits), "interval 应兜底索引漏标文件"
    finally:
        mgr.close()


def test_interval_disabled_by_default(tmp_path):
    """watch_interval_seconds=0(默认)→ 不起 interval timer。"""
    mgr = _mgr(tmp_path)  # enable_watcher=False, watch_interval_seconds 默认 0
    assert mgr._interval_timer is None  # noqa: SLF001
    mgr.close()
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_memory_store.py::test_interval_sync_reindexes_missed_change tests/test_memory_store.py::test_interval_disabled_by_default -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'watch_interval_seconds'`

- [ ] **Step 3: 加 config schema**

`twinkle/config/schema.py` `MemoryIndexConfig`（125-126 行）：

```python
class MemoryIndexConfig(_StrictModel):
    debounce_seconds: float = 2.0  # 写后去抖窗口:连续写塌成一次重索引(对齐 jiuwenswarm watchDebounceMs)
    watch_interval_seconds: float = 0.0  # interval 兜底全扫间隔(0=关,对齐 j/openclaw 默认关);防 watchdog 漏标
```

`twinkle/config/__init__.py:51` 后加：

```python
MEMORY_INDEX_DEBOUNCE_SECONDS = settings.memory.index.debounce_seconds
MEMORY_WATCH_INTERVAL_SECONDS = settings.memory.index.watch_interval_seconds
```

`twinkle/resources/config.yaml` memory.index 段（找现有 `debounce_seconds` 行附近）加注释行（具体行号实现时定位）：

```yaml
  index:
    debounce_seconds: 2.0    # 写后去抖窗口
    watch_interval_seconds: 0  # interval 兜底全扫间隔(0=关);高可靠场景可设 300
```

- [ ] **Step 4: 实现 `__init__` kwarg + `_ensure_interval_sync`**

`store.py` `__init__` 签名加 `watch_interval_seconds: float = 0.0`，存 `self._watch_interval = watch_interval_seconds`。`__init__` 末尾（`if enable_watcher: self._start_watcher()` 后）加 `self._ensure_interval_sync()`。

`_start_watcher` 后加方法：

```python
    def _ensure_interval_sync(self) -> None:
        """起 interval 定时全扫(默认关)。防 watchdog 漏标:遍历 list_files() 白名单
        逐个 _index_file(指纹跳过未变,只重建变了的)。对齐 jiuwenswarm intervalMinutes /
        openclaw ensureIntervalSync(均默认关)。"""
        if self._watch_interval <= 0:
            return
        if self._interval_timer is not None:
            return

        def _interval_loop() -> None:
            if self._observer is None and self._interval_timer is None:
                return  # 已 close
            try:
                for rel in self.list_files():
                    self._index_file(rel)
            except Exception:
                log.exception("interval sync failed")
            # 重排下一次(若未 close)
            if self._interval_timer is not None:
                self._interval_timer = threading.Timer(
                    self._watch_interval, _interval_loop)
                self._interval_timer.start()

        self._interval_timer = threading.Timer(self._watch_interval, _interval_loop)
        self._interval_timer.daemon = True
        self._interval_timer.start()
```

`close()` 已在 Task 3 取消 `_interval_timer`，无需改。

`twinkle/agentserver/memory/__init__.py` `get_memory_manager` 构造加 `watch_interval_seconds=MEMORY_WATCH_INTERVAL_SECONDS`（import `MEMORY_WATCH_INTERVAL_SECONDS`）。

- [ ] **Step 5: 验证通过**

Run: `python -m pytest tests/test_memory_store.py::test_interval_sync_reindexes_missed_change tests/test_memory_store.py::test_interval_disabled_by_default -v`
Expected: 2 个 PASS

- [ ] **Step 6: 跑全测试**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS

- [ ] **Step 7: Commit（先问用户）**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml twinkle/agentserver/memory/store.py twinkle/agentserver/memory/__init__.py tests/test_memory_store.py
git commit -m "feat(memory): add interval fallback sync (default off, config-gated)"
```

---

## Task 7: 删 write/edit/replace 的 `_mark_dirty` + 改测试触发方式

**核心破坏性改动**。Task 1-6 watcher 已可用，此时删 write 的 `_mark_dirty` 只去冗余触发。但现有测试靠"write 自带 dirty + search 兜底搜到"——删后 dirty 空需改触发方式。

**Files:**
- Modify: `twinkle/agentserver/memory/store.py:180,199,221`
- Modify: `tests/test_memory_store.py`（审计改触发方式）

### Task 7a: 审计改测试触发方式（先改测试，此时 write 仍 mark_dirty，双触发测试也过）

- [ ] **Step 1: 列出靠 dirty 的测试**

靠"write 后 search 兜底搜到刚写内容"或"write 后 `_flush_now`"的测试（删 write mark_dirty 后 dirty 空，会挂）：

- `test_write_does_not_index_until_flush`（94-111）
- `test_search_fts_only_hits_written_fact`（114-122）
- `test_search_fts_only_miss`（124-127）
- `test_search_fts_phrase_bug_regression`（130-139）
- `test_search_fts_jieba_word_level`（142-150）
- `test_search_fts_degraded_no_jieba`（153-169）
- `test_search_logs`（172-176）
- `test_edit_replaces_and_reindexes`（216-224）
- `test_replace_full_overwrite_and_reindexes`（234-248）
- `test_hybrid_search_runs_with_sqlite_vec`（193-203）
- `test_mtv_degrades_to_fts_only_when_no_provider`（206-213）
- `test_hybrid_search_result_has_line_numbers`（317-328）
- `test_model_change_clears_index`（251-262）— 已 `_flush_now`，但 `_flush_now` 前需 dirty 非空
- `test_fifo_cap_evicts_oldest`（265-273）— 同上

- [ ] **Step 2: 统一改法**

这些测试 `_mgr(tmp_path)`（`enable_watcher=False`）→ write 不标 dirty（删后）→ 需显式标。改法：write 后加 `mgr._mark_dirty("MEMORY.md")` 模拟 watchdog 标 dirty，再 search/`_flush_now`。

示例改 `test_search_fts_only_hits_written_fact`（114-122）：

```python
def test_search_fts_only_hits_written_fact(tmp_path):
    """无 embed_provider → FTS-only；写一条事实，按关键词搜索，命中。"""
    mgr = _mgr(tmp_path)  # enable_watcher=False
    mgr.write("MEMORY.md", "用户偏好用中文回答问题。", append=True)
    mgr.write("MEMORY.md", "项目架构是两进程 WebSocket。", append=True)
    mgr._mark_dirty("MEMORY.md")  # noqa: SLF001 — 模拟 watchdog 标 dirty
    hits = mgr.search("偏好")
    assert hits
    assert any("偏好" in h["text"] for h in hits)
```

对**已调 `_flush_now()`** 的测试（test_model_change、test_fifo_cap），在 `_flush_now()` 前加 `mgr._mark_dirty("MEMORY.md")`。

对 `test_write_does_not_index_until_flush`（94-111）：此测试语义即"write 零索引 + search 兜底"。删 write mark_dirty 后，write 真零触发（连 dirty 都不标），search 兜底依赖 dirty——需 `mgr._mark_dirty("MEMORY.md")` 模拟 watcher 标 dirty 后 search 兜底。改：

```python
def test_write_does_not_index_until_flush(tmp_path):
    """write 只落盘不标 dirty(对齐 j/openclaw 模型 B);dirty 由 watcher 标。
    这里模拟 watcher 标 dirty 后 search 兜底索引。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    # write 零索引 + 零 dirty(模型 B)→ DB 无索引
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    mgr._mark_dirty("MEMORY.md")  # noqa: SLF001 — 模拟 watcher 标 dirty
    hits = mgr.search("偏好")  # search 兜底:if dirty 同步索引后搜到
    assert any("偏好" in h["text"] for h in hits)
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] >= 1
```

对 `test_search_logs`（172-176）：`_mgr(tmp_path).search("x")` — 无 write 无 dirty，search 空返 + log。不受影响（不依赖 dirty）。**不用改**。

逐个按上述模式改（除 test_search_logs 外，其余 13 个都加 `_mark_dirty`）。

- [ ] **Step 3: 验证测试在 write 仍 mark_dirty 下通过（双触发幂等）**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS（write 仍 `_mark_dirty`，测试加的 `_mark_dirty` 是二次，set 去重幂等无害）

### Task 7b: 删 write/edit/replace 的 `_mark_dirty`

- [ ] **Step 4: 删三处调用**

`twinkle/agentserver/memory/store.py`：

删 `write` 的 180 行 `self._mark_dirty(relative_path)`：
```python
        except OSError as exc:
            return f"Error writing '{path}': {exc}"
        log.info("write_memory path=%s append=%s", relative_path, append)
        return f"Stored to {relative_path}."
```

删 `edit` 的 199 行：
```python
        fpath.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        log.info("edit_memory path=%s", relative_path)
        return f"Edited {relative_path}."
```

删 `replace` 的 221 行：
```python
        except OSError as exc:
            tmp.unlink(missing_ok=True)  # 别留 .tmp 残留
            return f"Error replacing '{path}': {exc}"
        log.info("replace_memory path=%s", relative_path)
        return f"Replaced {relative_path}."
```

同时更新 `store.py:66-70` 那段注释（"省 watchdog：write 在 manager 内直接 mark_dirty"）——现在反过来，改写：

```python
        # 模型 B(对齐 jiuwenswarm/openclaw):write/edit/replace 只落盘,不标 dirty。
        # dirty 完全由 watchdog 事件标(_MemoryEventHandler.on_modified/created/moved)。
        # search 兜底(if dirty: _flush_now)+ interval(默认关)兜漏。watchdog 复用
        # _mark_dirty 的 set+Timer 防抖合并 OS 事件噪音。check_same_thread=False +
        # RLock:_flush_dirty 跑在 timer 线程/emitter 线程,与主线程 search 并发。
```

- [ ] **Step 5: 验证全测试通过**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS（测试已在 7a 加 `_mark_dirty` 模拟 watcher；write 不标 dirty 不影响）

- [ ] **Step 6: 跑 memory 相关全量测试 + 其他可能受影响的**

Run: `python -m pytest tests/test_memory_store.py tests/test_memory_integration.py tests/test_memory_wiring.py -v 2>&1 | tail -30`
Expected: 全 PASS。若有其他测试文件直接构造 MemoryManager 且靠 write 标 dirty，按 7a 模式加 `_mark_dirty` 或 `enable_watcher=True`。

- [ ] **Step 7: Commit（先问用户）**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "refactor(memory): remove write-path _mark_dirty, rely on watchdog (model B)"
```

---

## Task 8: 收尾验证（全量测试 + 降级路径）

**Files:** 无新改动，全量验证。

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v 2>&1 | tail -40`
Expected: 无 regression（cron/pptx 的 pre-existing 环境失败除外，见 [[phase6-cron-tests-environmental-failures]]）

- [ ] **Step 2: 降级路径手测（watchdog 不装场景）**

Run: `pip uninstall watchdog -y && python -c "from twinkle.agentserver.memory.store import MemoryManager; m=MemoryManager('/tmp/mm_test', enable_watcher=True); print('observer:', m._observer); m.close()" && pip install watchdog`
Expected: 输出 `observer: None`（降级无 watcher）+ log warning，不崩。

- [ ] **Step 3: 更新记忆**

写/更新记忆 `[[memory-store-index-debounce]]`：注明 watchdog 反向触发已落地（模型 B），write 不再 mark_dirty，dirty 由 watcher 标 + search if-dirty 兜底 + interval 默认关。

- [ ] **Step 4: Commit 收尾（先问用户）**

```bash
git add -A
git commit -m "docs(memory): update notes for watchdog reverse-trigger model B"
```

---

## Self-Review

**1. Spec coverage:**
- 删 write/edit/replace mark_dirty → Task 7 ✓
- watchdog on_modified/created/moved/deleted + 白名单 → Task 3 ✓
- on_moved 取 dest（replace rename）→ Task 3 handler + Task 4 测试 ✓
- on_deleted → _remove_file_from_index → Task 2 + Task 3/4 ✓
- dirty 保留 set → 不改 set，Task 3 复用 _mark_dirty ✓
- search if-dirty 兜底保留 → 不改 search，Task 7 测试验证 ✓
- interval 默认关可配置开 → Task 6 ✓
- close() + atexit + _set_memory_manager close old → Task 3 + Task 5 ✓
- enable_watcher kwarg + helper 默认 False → Task 3 Step 1 ✓
- 测试影响（审计改触发方式）→ Task 7a ✓
- watchdog 依赖 → Task 1 ✓
- 降级（import 失败）→ Task 3 test_watcher_degrades + Task 8 Step 2 ✓

**2. Placeholder scan:** 无 TBD/TODO。Task 6 config.yaml 行号"实现时定位"——config.yaml 的 memory.index 段确切位置未读，但给了确切 yaml 内容。Task 7a 测试列表完整。无"add appropriate error handling"之类。

**3. Type consistency:** `enable_watcher`/`watch_interval_seconds`/`_observer`/`_interval_timer`/`_remove_file_from_index`/`close()`/`_start_watcher`/`_ensure_interval_sync` 在各 task 一致。`_MemoryEventHandler._on(abs_path, is_delete=False)` 签名一致。`get_memory_manager` 传参 `enable_watcher=True` + `watch_interval_seconds` 一致。

**4. 风险已记**：Task 7 测试审计是最大工作量；删 mark_dirty 后降级模式（watcher 不可用）write 不标 dirty——Task 8 Step 2 验证降级不崩。
