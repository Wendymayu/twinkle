# Memory 召回锚点恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **注意：用户要求全程不 commit，改动保留在工作区。每个 Task 末尾不执行 git commit。**

**Goal:** 让 memory_search 召回的每个 chunk 带回"所属对话日期锚点"，chunk 切分不跨 `## YYYY-MM-DD` 节边界，返回头跟随 query 语言自适应中英文——修复 conv-26 cat2 评测中"相对日期丢精度"类低分（#33/#36/#54/#57 等）。

**Architecture:** 三块正交改动——(A) `store.search` 返回 hit 带新字段 `section_title`（解析行号→最近 `## ` 标题，daily 用文件名日期）；(B) `store._chunk` 按 `## ` 节边界切，标题行强制为 chunk 首行；(E) `memory_search` 返回头 CJK 检测自适应 + `memory_hook` 策略 prompt 加"作答跟随提问语言"约束。不改 FTS/vec 融合权重，不改 dreaming（只输出 JSON 行号）。

**Tech Stack:** Python 3.12、SQLite + FTS5 + sqlite-vec、pytest（`asyncio.run()` + `free_port`，无 pytest-asyncio）。

**Spec:** `docs/superpowers/specs/2026-09-20-memory-recall-anchor-recovery-design.md`

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `twinkle/agentserver/memory/store.py` | 记忆存储 + 检索 | 改 `_chunk`（B，~L462）；新增 `_section_title_from_lines` staticmethod（A）；改 `search` 按 path 缓存读盘 + hit 带 `section_title`（A） |
| `twinkle/agentserver/tools/builtin/memory_tools.py` | memory_search 工具薄封装 | 改 `memory_search` 返回格式加 `[§ anchor]` + 行号 + 返回头语言自适应（E） |
| `twinkle/agentserver/hooks/builtin/memory_hook.py` | before_invoke 注策略 prompt | `_PROMPT_TEMPLATE` 末尾加作答语言约束（E） |
| `tests/test_memory_store.py` | store 单元测试 | 加 `_chunk` 按节切 + `_section_title_from_lines` + search 返回 section_title 测试 |
| `tests/test_memory_tools.py` | memory_search 工具测试 | 加返回格式锚点 + 返回头语言测试 |
| `tests/test_memory_hook.py` | hook prompt 测试 | 加作答语言约束断言 |

---

### Task 1: B — `_chunk` 按节切分（不跨 `## ` 标题）

**Files:**
- Modify: `twinkle/agentserver/memory/store.py:462-484`（`_chunk` 方法）
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_memory_store.py` 末尾：

```python
def test_chunk_does_not_cross_section_header(tmp_path):
    """_chunk 按节切:`## ` 标题行不落在 chunk 中间,是某 chunk 首行——
    保证对话日期标题与该节事实同 chunk,召回不丢锚点。"""
    mgr = _mgr(tmp_path)
    content = "## 2023-08-23 — Conv\n- fact A\n- fact B\n## 2023-08-25 — Conv\n- fact C\n"
    chunks = mgr._chunk(content)  # noqa: SLF001
    for c in chunks:
        lines = c.text.splitlines()
        # 任一 chunk 内部(非首行)不得含 ## 标题
        assert not any(ln.lstrip().startswith("## ") for ln in lines[1:]), \
            f"chunk L{c.start}-{c.end} 内部含 ## 标题(非首行),切断锚点"
    first_lines = [c.text.splitlines()[0] for c in chunks if c.text.splitlines()]
    assert any("2023-08-23" in fl for fl in first_lines)
    assert any("2023-08-25" in fl for fl in first_lines)


def test_chunk_no_header_falls_back_to_original(tmp_path):
    """无 `## ` 标题(USER.md 风格)走原 budget+overlap 逻辑,不丢 chunk。"""
    mgr = _mgr(tmp_path)
    content = "\n".join(f"- item {i} has unique content number {i}" for i in range(30))
    chunks = mgr._chunk(content)  # noqa: SLF001
    assert len(chunks) >= 2
    assert all(not ln.lstrip().startswith("## ")
               for c in chunks for ln in c.text.splitlines())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_store.py::test_chunk_does_not_cross_section_header tests/test_memory_store.py::test_chunk_no_header_falls_back_to_original -v`
Expected: 两个 FAIL（旧 `_chunk` 会跨 `## `，第一个断言失败）。

- [ ] **Step 3: Implement `_chunk` 按节切**

替换 `twinkle/agentserver/memory/store.py:462-484` 的 `_chunk` 方法：

```python
    def _chunk(self, content: str) -> list[Chunk]:
        lines = content.splitlines()
        if not lines:
            return []
        budget = max(1, self._chunk_tokens) * 3
        overlap = max(0, self._chunk_overlap) * 3
        out: list[Chunk] = []
        i, n = 0, len(lines)
        while i < n:
            size, j = 0, i
            while j < n and size < budget:
                # 节边界:遇到 `## ` 标题且非当前 chunk 首行 → 截断,
                # 标题行留给下一 chunk 作首行(不跨节,保对话日期锚点)。
                if j > i and lines[j].lstrip().startswith("## "):
                    break
                size += len(lines[j]) + 1
                j += 1
            out.append(Chunk(i + 1, j, "\n".join(lines[i:j])))
            if j >= n:
                break
            # 若因 ## 截断 → 下一 chunk 从 ## 行起(无跨节 overlap)
            if lines[j].lstrip().startswith("## "):
                i = j
                continue
            # 节内 overlap 回退(原逻辑)
            overlap_bytes, backtrack_to = 0, j
            while backtrack_to > i + 1 and overlap_bytes < overlap:
                backtrack_to -= 1
                overlap_bytes += len(lines[backtrack_to]) + 1
            i = backtrack_to if backtrack_to > i else j
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_store.py::test_chunk_does_not_cross_section_header tests/test_memory_store.py::test_chunk_no_header_falls_back_to_original -v`
Expected: PASS。

- [ ] **Step 5: 回归 store 全量测试不破**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS（chunk 切分变可能改变某些断言 chunk 数，但现有测试只断言"命中/不命中"不数 chunk，应全绿；若 `test_fifo_cap_evicts_oldest` 等因切分变 chunk 数变化，检查是否仍满足 cap）。

- [ ] **Step 6: 不 commit**（用户要求），保留改动。

---

### Task 2: A — `_section_title_from_lines` + search 返回带锚点

**Files:**
- Modify: `twinkle/agentserver/memory/store.py`（新增 `_section_title_from_lines` staticmethod；改 `search` ~L599-643 按 path 缓存 + hit 带 `section_title`）
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_memory_store.py` 末尾：

```python
def test_section_title_finds_recent_header():
    """_section_title_from_lines:从 start_line 向上找最近 `## ` 标题行。"""
    from twinkle.agentserver.memory.store import MemoryManager
    lines = ["## 2023-08-23 — Conv", "- fact A", "- fact B"]
    assert MemoryManager._section_title_from_lines(lines, 2, "MEMORY.md") == "## 2023-08-23 — Conv"
    assert MemoryManager._section_title_from_lines(lines, 3, "MEMORY.md") == "## 2023-08-23 — Conv"


def test_section_title_daily_filename_fallback():
    """daily 文件无 `## ` → 用文件名日期作锚点。"""
    from twinkle.agentserver.memory.store import MemoryManager
    assert MemoryManager._section_title_from_lines(["- today fact"], 1,
                                                    "daily_memory/2023-08-23.md") == "2023-08-23"


def test_section_title_user_md_fallback():
    """USER.md 无 `## ` → 返回 'User Profile'。"""
    from twinkle.agentserver.memory.store import MemoryManager
    assert MemoryManager._section_title_from_lines(["- x"], 1, "USER.md") == "User Profile"


def test_section_title_empty_when_no_header_no_fallback():
    """无 `## ` 且非 daily/USER.md → 空串。"""
    from twinkle.agentserver.memory.store import MemoryManager
    assert MemoryManager._section_title_from_lines(["- x"], 1, "MEMORY.md") == ""


def test_search_returns_section_title_field(tmp_path):
    """search 返回 hit 含 section_title 字段(MEMORY.md 命中时非空)。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "## 2023-08-23 — Conv\n- Caroline applied to adoption this week\n",
              append=True)
    mgr._mark_dirty("MEMORY.md")  # noqa: SLF001 — 模拟 watcher 标 dirty
    hits = mgr.search("adoption")
    assert hits
    assert hits[0]["section_title"]
    assert "2023-08-23" in hits[0]["section_title"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_store.py -k "section_title or search_returns_section_title_field" -v`
Expected: FAIL（`_section_title_from_lines` 不存在 / hit 无 `section_title` 字段）。

- [ ] **Step 3: Add `_section_title_from_lines` staticmethod**

在 `store.py` 的 `_hit` staticmethod（~L668）之后追加：

```python
    @staticmethod
    def _section_title_from_lines(lines: list[str], start_line: int, path: str) -> str:
        """chunk 所属的最近上一个 `## ` 标题行(给召回补对话日期锚点)。

        从 start_line(1-indexed)向上扫,返回首个 `## ` 行 strip;无则 fallback:
        daily 文件用文件名日期(2023-08-23)、USER.md 返回 'User Profile'、其余空串。
        lines 是已读的文件行列表(由 search 按 path 缓存传入,避免每 hit 读盘)。"""
        for k in range(min(start_line - 1, len(lines) - 1), -1, -1):
            if lines[k].lstrip().startswith("## "):
                return lines[k].strip()
        if path.startswith("daily_memory/"):
            tail = path[len("daily_memory/"):]
            return tail[:-3] if tail.endswith(".md") else tail
        if path == "USER.md":
            return "User Profile"
        return ""
```

- [ ] **Step 4: Modify `search` to cache file lines + populate `section_title`**

替换 `store.py:599-643` 的 `search` 方法（从 `def search` 到 `return [self._hit(...)] for ... in scored]` 整段）：

```python
    def search(self, query: str, max_results: int | None = None) -> list[dict]:
        """混合检索,排序 + top-N 截断(无分数截断,对齐 jiuwenswarm 的无截断
        FTS)。纯 FTS(无 sqlite-vec 或无 provider):按 SQL bm25 顺序匹配
        (最优在前)。混合:融合向量相似度 + FTS bm25 分(两者越大越优)按
        融合分排序。每个 hit 带 section_title(所属 `## ` 对话日期锚点)。

        按 path 缓存读盘:本次 search 的 hit 跨多 path 时,每 path 只读一次
        文件文本,所有 hit 共用调 _section_title_from_lines。"""
        if self._dirty_paths:
            self._flush_dirty()
        max_results = max_results or self._max_results
        candidates = min(200, max(1, int(max_results * self._candidate_multiplier)))
        # 按 path 缓存文件行(避免每 hit read 一次盘)
        file_lines_cache: dict[str, list[str]] = {}

        def _lines_for(path: str) -> list[str]:
            if path not in file_lines_cache:
                text = self.read(path)
                file_lines_cache[path] = [] if text.startswith("Error:") else text.splitlines()
            return file_lines_cache[path]

        def _hit_anchored(row, score: float) -> dict:
            lines = _lines_for(row["path"])
            st = MemoryManager._section_title_from_lines(lines, row["start_line"], row["path"])
            return {"path": row["path"], "score": round(score, 4),
                    "text": row["text"], "start_line": row["start_line"],
                    "end_line": row["end_line"], "section_title": st}

        with self._db_lock:
            fts_rows = self._fts_search(query, candidates)  # ORDER BY bm -> 最优在前
            fts_by_rowid = {r["rowid"]: r for r in fts_rows}

            if not (self._vec_enabled and self._provider is not None):
                hits = fts_rows[:max_results]
                log.info("memory_search query=%r hits=%d (fts-only)", query, len(hits))
                return [_hit_anchored(r, self._text_sim(r["bm"])) for r in hits]

            vec_sims = self._vec_search(query, candidates)  # {rowid: 相似度}

            candidate_rows = dict(fts_by_rowid)
            for row_id in vec_sims:
                if row_id not in candidate_rows:
                    cr = self._db.execute(
                        "SELECT rowid, path, text, start_line, end_line "
                        "FROM chunks WHERE rowid=?", (row_id,)).fetchone()
                    if cr:
                        candidate_rows[row_id] = cr

            scored = []
            for row_id, row in candidate_rows.items():
                text_sim = self._text_sim(fts_by_rowid[row_id]["bm"]) if row_id in fts_by_rowid else 0.0
                vec_sim = vec_sims.get(row_id, 0.0)
                fused = self._vector_weight * vec_sim + self._text_weight * text_sim
                scored.append((row, fused))

            scored.sort(key=lambda x: x[1], reverse=True)
            scored = scored[:max_results]
            log.info("memory_search query=%r hits=%d (hybrid)", query, len(scored))
            return [_hit_anchored(row, score) for row, score in scored]
```

> 注：旧的 `_hit` staticmethod 保留（其他地方未用，但保留以防外部引用；如 lint 报未使用再删）。`_text_sim` 不变。

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_store.py -k "section_title or search_returns_section_title_field" -v`
Expected: PASS。

- [ ] **Step 6: 回归 store 全量 + 形状契约**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS。重点看 `test_hybrid_search_result_has_line_numbers`（返回形状契约）——新增 `section_title` 字段不破坏 `start_line`/`end_line` 断言。

- [ ] **Step 7: 不 commit**（用户要求），保留改动。

---

### Task 3: memory_search 返回格式加锚点 + 行号

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/memory_tools.py:12-22`（`memory_search`）
- Test: `tests/test_memory_tools.py`

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_memory_tools.py` 末尾：

```python
def test_memory_search_returns_anchor_and_line_range(isolated_memory):
    """memory_search 返回含 [§ section_title] 锚点 + L{start}-{end} 行号,
    让 agent 能定位"这是哪次对话/哪段"——治召回孤立片段无上下文。"""
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func(
        "MEMORY.md", "## 2023-08-23 — Conv\n- Caroline applied to adoption this week", True))
    out = asyncio.run(memory_search.func("adoption"))
    assert "§" in out, "返回应含 [§ anchor] 锚点"
    assert "2023-08-23" in out, "锚点应含对话日期"
    assert "L1-" in out, "返回应含行号范围"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_tools.py::test_memory_search_returns_anchor_and_line_range -v`
Expected: FAIL（旧格式 `### {path} (score)` 无 § 锚点、无行号）。

- [ ] **Step 3: Implement memory_search 返回格式**

替换 `twinkle/agentserver/tools/builtin/memory_tools.py:12-22` 的 `memory_search`：

```python
@tool
async def memory_search(query: str, max_results: int | None = None) -> str:
    """搜索长期记忆中相关的事实。当答案依赖跨 session 的用户偏好、
    历史或过往决策时调用。"""
    hits = get_memory_manager().search(query, max_results=max_results)
    if not hits:
        return "No relevant memories found."
    lines = [f"## 记忆召回 ({len(hits)} 条)"]
    for h in hits:
        anchor = h.get("section_title", "")
        anchor_tag = f" [§ {anchor}]" if anchor else ""
        lines.append(
            f"### {h['path']} L{h['start_line']}-{h['end_line']}{anchor_tag} "
            f"(score {h['score']})\n{h['text']}")
    return "\n\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_tools.py::test_memory_search_returns_anchor_and_line_range -v`
Expected: PASS。

- [ ] **Step 5: 回归 memory_tools 全量**

Run: `python -m pytest tests/test_memory_tools.py -v`
Expected: 全 PASS（`test_write_read_search_round_trip` 断言 `"偏好" in hits` 仍成立——hits 是格式化字符串，含"偏好"即可）。

- [ ] **Step 6: 不 commit**（用户要求），保留改动。

---

### Task 4: E — memory_search 返回头语言自适应

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/memory_tools.py`（`memory_search` 返回头 + 新增 `_recall_header` helper）
- Test: `tests/test_memory_tools.py`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_memory_tools.py` 末尾：

```python
def test_memory_search_header_english_for_english_query(isolated_memory):
    """英文 query(无 CJK)→ 英文返回头,不诱导 agent 中文作答(#73 根因之一)。"""
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "## 2023-08-23\n- applied to adoption", True))
    out = asyncio.run(memory_search.func("adoption"))
    assert "## Memory Recall" in out
    assert "记忆召回" not in out


def test_memory_search_header_chinese_for_cjk_query(isolated_memory):
    """中文 query(含 CJK)→ 中文返回头。"""
    from twinkle.agentserver.tools.builtin.memory_tools import memory_search, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "- 用户偏好中文", True))
    out = asyncio.run(memory_search.func("偏好"))
    assert "## 记忆召回" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_tools.py -k "header_english or header_chinese" -v`
Expected: FAIL（当前返回头恒为中文 `## 记忆召回`，英文 query 的 `assert "记忆召回" not in out` 失败）。

- [ ] **Step 3: Add `_recall_header` helper + 用到 memory_search**

在 `twinkle/agentserver/tools/builtin/memory_tools.py` 顶部 import 区后加：

```python
import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _recall_header(query: str, n: int) -> str:
    """返回头跟随 query 语言:CJK→中文,否则英文。避免英文数据评测时中文头
    混入 context 诱导中文作答(治 #73)。"""
    if _CJK_RE.search(query):
        return f"## 记忆召回 ({n} 条)"
    return f"## Memory Recall ({n} hits)"
```

并把 Task 3 改的 `memory_search` 里 `lines = [f"## 记忆召回 ({len(hits)} 条)"]` 一行改为：

```python
    lines = [_recall_header(query, len(hits))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_tools.py -k "header_english or header_chinese" -v`
Expected: PASS。

- [ ] **Step 5: 回归 memory_tools 全量**

Run: `python -m pytest tests/test_memory_tools.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 不 commit**（用户要求），保留改动。

---

### Task 5: E — memory_hook 策略 prompt 加作答语言约束

**Files:**
- Modify: `twinkle/agentserver/hooks/builtin/memory_hook.py:13-27`（`_PROMPT_TEMPLATE`）
- Test: `tests/test_memory_hook.py`

- [ ] **Step 1: Write the failing test**

先确认 `tests/test_memory_hook.py` 是否有 `_build_prompt` 测试结构；若无对应断言，追加：

```python
def test_memory_hook_prompt_has_answer_language_constraint():
    """策略 prompt 含'作答跟随提问语言'约束——治 #73 中文作答英文 gold。"""
    from twinkle.agentserver.hooks.builtin.memory_hook import _build_prompt
    prompt = _build_prompt()
    assert "作答" in prompt
    assert "语言" in prompt
    # 明确约束:跟随提问语言
    assert "提问" in prompt or "问" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_hook.py::test_memory_hook_prompt_has_answer_language_constraint -v`
Expected: FAIL（当前 `_PROMPT_TEMPLATE` 无作答语言约束句）。

- [ ] **Step 3: Add 作答语言约束到 `_PROMPT_TEMPLATE`**

在 `twinkle/agentserver/hooks/builtin/memory_hook.py` 的 `_PROMPT_TEMPLATE` 字符串末尾（`recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。` 之后、闭合 `"""` 之前）追加：

```
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。

**作答语言**:用用户提问的语言作答(英文问→英文答,中文问→中文答),不要固定用某种语言——记忆内容是英文就用英文答,是中文就用中文答。
```

即把 `_PROMPT_TEMPLATE` 最后一段从：

```python
不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""
```

改为：

```python
不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。

**作答语言**:用用户提问的语言作答(英文问→英文答,中文问→中文答),不要固定用某种语言——记忆内容是英文就用英文答,是中文就用中文答。"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_hook.py::test_memory_hook_prompt_has_answer_language_constraint -v`
Expected: PASS。

- [ ] **Step 5: 回归 memory_hook 全量**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 不 commit**（用户要求），保留改动。

---

### Task 6: 全量回归 + 评测重建验证

**Files:**
- 无代码改动，只跑测试 + 评测重建

- [ ] **Step 1: 全量 memory 测试套件回归**

Run: `python -m pytest tests/test_memory_store.py tests/test_memory_tools.py tests/test_memory_hook.py tests/test_memory_integration.py -v`
Expected: 全 PASS。若有失败，回到对应 Task 修复。

- [ ] **Step 2: 全量 twinkle 测试不破**

Run: `python -m pytest tests/ -v --ignore=tests/test_memory_embeddings.py -q`
Expected: 无新增失败（`test_memory_embeddings.py` 依赖 bge-m3 模型可能慢/需网络，可单独跑；其余全绿）。

- [ ] **Step 3: 评测重建——删旧 memory.db 触发重索引**

Run（在 conv-26 输出目录）:
```bash
rm "D:/code/opensource/github/data-sets/locomo_twinkle/output/2026-09-20_125942/conv-26/memory/memory.db"
```
说明：改了 `_chunk` 逻辑后旧 db 的 chunks 是旧切分（跨节版）。下次 MemoryManager 初始化会重建 schema，但 chunks 内容要靠 watchdog 标 dirty + `_flush_dirty` 重索引——删 db 强制全量重建。评测脚本重新 ingest 时会重建。

- [ ] **Step 4: 重跑 conv-26 cat2 评测验证 F1 提升**

按 locomo_twinkle 评测脚本重跑 conv-26 `--category 2`（`--concurrency 1` 控非确定性）。对比 baseline mean F1 0.578，看"相对日期丢精度"类题（#33/#36/#54/#57/#8/#21/#31/#35/#41/#45/#67/#5/#28/#29）mean F1 是否提升（目标 +0.05~0.15；gold 相对表述天花板仍在，不会到 0.9）。

> 注：此步依赖 locomo_twinkle 评测 harness（非 twinkle 仓库），具体命令见该仓库 README。如 harness 未配好，至少验证 Task 1-5 的单元/集成测试全绿 + 人工抽检 memory_search 返回含锚点。

- [ ] **Step 5: 不 commit**（用户要求），改动保留在工作区供你 review。

---

## Self-Review

**1. Spec coverage:**
- §4.1 B `_chunk` 按节切 → Task 1 ✓
- §4.2 A `_section_title_from_lines` + search 按 path 缓存 + hit 带 section_title → Task 2 ✓
- §4.3 memory_tools 返回格式加锚点 + 行号 → Task 3 ✓
- §4.4 E memory_search 返回头语言自适应 → Task 4 ✓
- §4.4 E memory_hook 策略 prompt 加作答语言约束 → Task 5 ✓
- §5 重索引（删 memory.db）→ Task 6 Step 3 ✓
- §6 测试（单元 + 评测回归）→ Task 1-5 单元 + Task 6 评测 ✓
- §7 不做（rerank/ingest 改 hook 双写/harness 抽答/`_CHUNK_LOGIC_VERSION`/专有名词保真）→ 均未出现在 plan ✓

**2. Placeholder scan:** 无 TBD/TODO；每个 step 含实际可运行代码或确切命令。Task 6 Step 4 标注"依赖 locomo_twinkle harness"是诚实边界，非 placeholder。

**3. Type consistency:**
- `_section_title_from_lines(lines, start_line, path)` 在 Task 2 定义、`search` 内调用、测试调用——签名一致 ✓
- `_hit_anchored(row, score)` 在 `search` 内闭包定义，返回 dict 含 `section_title` ✓
- `_recall_header(query, n)` Task 4 定义、memory_search 调用、测试间接验证 ✓
- hit dict 形状 `{path, score, text, start_line, end_line, section_title}` 与 Task 3 `memory_search` 读取的 `h['path']/h['start_line']/h['end_line']/h['score']/h.get('section_title')` 一致 ✓
