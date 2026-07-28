# Phase 5a Long-Term Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `memory.py` stub with a real long-term memory: markdown-authoritative storage + SQLite (`sqlite-vec` + FTS5) hybrid retrieval + 4 model-driven `@tool`s + a `MemoryHook` that injects a usage-strategy prompt.

**Architecture:** Model-driven (no auto-inject). `memory/` package (`MemoryManager` + embedding providers + singleton) mirrors `skills/`. `MemoryHook`(before_model_call) injects a "when to search/write" prompt (like `SkillHook`); `memory_search`/`write_memory`/`read_memory`/`edit_memory` are `@tool`s the model calls on demand. `agent_loop` zero structural change (the `LongTermMemory` stub param is removed).

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `sqlite-vec` (optional `[memory]` extra), `httpx` (embedding API), `pydantic` (config), `pytest` (no `pytest-asyncio` — use `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-07-27-long-term-memory-design.md` (committed `0b067d0`).

---

## File Structure

| File | Responsibility |
|---|---|
| `twinkle/agentserver/memory/embeddings.py` (new) | `EmbeddingProvider` Protocol + `OpenAICompatibleEmbeddingProvider` + `MockEmbeddingProvider` (test-only) |
| `twinkle/agentserver/memory/store.py` (new) | `MemoryManager`: 6-table SQLite schema, path whitelist, write/read/edit, mtime-incremental indexing, FTS + hybrid search, model-change rebuild, FIFO cap |
| `twinkle/agentserver/memory/__init__.py` (new) | `get_memory_manager()` singleton + `_set_memory_manager()` test hook |
| `twinkle/agentserver/tools/builtin/memory_tools.py` (new) | 4 `@tool`s wrapping `get_memory_manager()` |
| `twinkle/agentserver/hooks/builtin/memory_hook.py` (new) | `MemoryHook`(before_model_call) — injects usage-strategy prompt |
| `twinkle/agentserver/memory.py` | **delete** (stub → replaced by `memory/` package) |
| `twinkle/config/schema.py` (modify) | +`MemoryConfig` (nested) + `TwinkleConfig.memory` + `_derive_paths` MEMORY_DIR + permissions.tools 4 memory tools |
| `twinkle/config/__init__.py` (modify) | flatten `MEMORY_*` constants |
| `twinkle/resources/config.yaml` (modify) | +`memory:` block + `permissions.tools` memory entries |
| `twinkle/workspace.py` (modify) | `ensure_workspace_dir` creates `MEMORY_DIR` + `daily_memory/` |
| `twinkle/agentserver/tools/__init__.py` (modify) | `tool_manager()` registers 4 memory tools |
| `twinkle/agentserver/hooks/builtin/__init__.py` (modify) | export `MemoryHook` |
| `twinkle/agentserver/server.py` (modify) | `build_agent_loop` drop `memory`, `main()` adds `MemoryHook()` to hook list |
| `twinkle/agentserver/agent_loop.py` (modify) | drop `memory` param/import/`recall()` call |
| `pyproject.toml` (modify) | +`memory` optional dependency (`sqlite-vec`) |
| `tests/test_memory_*.py` (new, 5 files) | store / embeddings / hook / tools / integration |

**Build order rationale:** units built with injectable params first (embeddings → store → singleton → tools → hook), then config/wiring, then integration. Each task is independently testable.

---

## Task 1: Embedding providers

**Files:**
- Create: `twinkle/agentserver/memory/embeddings.py`
- Test: `tests/test_memory_embeddings.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_embeddings.py
from twinkle.agentserver.memory.embeddings import (
    MockEmbeddingProvider, OpenAICompatibleEmbeddingProvider,
)


def test_mock_is_deterministic():
    p = MockEmbeddingProvider(dims=8)
    a = p.embed(["hello", "world"])
    assert len(a) == 2
    assert len(a[0]) == 8 and len(a[1]) == 8
    # same input → same vector
    assert p.embed(["hello"])[0] == a[0]
    assert p.embed(["hello"])[0] != p.embed(["world"])[0]
    assert p.model == "mock" and p.dims == 8


def test_mock_empty():
    assert MockEmbeddingProvider(dims=4).embed([]) == []


def test_openai_compatible_parses_response(monkeypatch):
    calls = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self): pass

        def json(self):
            return {"data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]},
                             {"index": 0, "embedding": [0.4, 0.5, 0.6]}]}

    class FakeClient:
        def __init__(self, *a, **k): pass

        def __enter__(self): return self

        def __exit__(self, *a): pass

        def post(self, url, headers=None, json=None):
            calls["url"] = url
            calls["headers"] = headers
            calls["json"] = json
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    p = OpenAICompatibleEmbeddingProvider(
        base_url="https://api.example.com/v1", api_key="sk-x",
        model="text-embedding-3-small", dims=3)
    out = p.embed(["foo", "bar"])
    # sorted by index → [foo(0), bar(1)] order preserved
    assert out == [[0.4, 0.5, 0.6], [0.1, 0.2, 0.3]]
    assert calls["url"] == "https://api.example.com/v1/embeddings"
    assert calls["headers"]["Authorization"] == "Bearer sk-x"
    assert calls["json"]["model"] == "text-embedding-3-small"
    assert calls["json"]["input"] == ["foo", "bar"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_embeddings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.memory'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/memory/embeddings.py
"""Embedding providers for long-term memory.

OpenAICompatibleEmbeddingProvider = production (reuses LLM_BASE_URL + LLM_API_KEY).
MockEmbeddingProvider = TEST-ONLY (deterministic hash pseudo-vectors). Never used
as a production fallback — no-key degrades to FTS-only (see store.MemoryManager).
"""
from __future__ import annotations

import hashlib
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dims(self) -> int: ...
    @property
    def model(self) -> str: ...


class OpenAICompatibleEmbeddingProvider:
    """POST {base_url}/embeddings (OpenAI/DashScope/OpenRouter compatible)."""

    def __init__(self, base_url: str, api_key: str, model: str, dims: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])
        return [d["embedding"] for d in data]


class MockEmbeddingProvider:
    """TEST-ONLY. Deterministic md5-based pseudo-vectors. Not for production."""

    def __init__(self, dims: int = 1536, model: str = "mock") -> None:
        self._dims = dims
        self._model = model

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.md5(t.encode("utf-8")).digest()
            out.append([(h[i % len(h)] / 255.0) for i in range(self._dims)])
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_embeddings.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory/embeddings.py tests/test_memory_embeddings.py
git commit -m "memory: add embedding providers (OpenAI-compatible + test Mock)"
```

---

## Task 2: MemoryManager — schema, path whitelist, list_files, read

**Files:**
- Create: `twinkle/agentserver/memory/store.py`
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_store.py
import sqlite3
import pytest
from twinkle.agentserver.memory.store import MemoryManager


def _mgr(tmp_path, **kw):
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)


def test_schema_creates_six_tables(tmp_path):
    mgr = _mgr(tmp_path)
    db = mgr._db  # noqa: SLF001 — test inspects internal handle
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    for t in ("chunks", "chunks_fts", "embedding_cache", "files", "meta"):
        assert t in names


def test_validate_path_whitelist(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr._validate_path("USER.md") == "USER.md"               # noqa: SLF001
    assert mgr._validate_path("MEMORY.md") == "MEMORY.md"
    assert mgr._validate_path("daily_memory/2026-07-27.md") == "daily_memory/2026-07-27.md"
    assert mgr._validate_path("../escape.md") is None
    assert mgr._validate_path("sub/dir/MEMORY.md") is None
    assert mgr._validate_path("daily_memory/notadate.md") is None
    assert mgr._validate_path("daily_memory/2026-07-27.txt") is None


def test_list_files_empty(tmp_path):
    assert _mgr(tmp_path).list_files() == []


def test_read_not_found(tmp_path):
    out = _mgr(tmp_path).read("USER.md")
    assert "not found" in out.lower()


def test_read_invalid_path(tmp_path):
    out = _mgr(tmp_path).read("../etc/passwd")
    assert "invalid" in out.lower()


def test_read_with_offset_limit(tmp_path):
    mgr = _mgr(tmp_path)
    (tmp_path / "MEMORY.md").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    assert mgr.read("MEMORY.md") == "L1\nL2\nL3\nL4\nL5"
    assert mgr.read("MEMORY.md", offset=1, limit=2) == "L2\nL3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.memory.store'`

- [ ] **Step 3: Write minimal implementation**

```python
# twinkle/agentserver/memory/store.py
"""MemoryManager — markdown-authoritative + SQLite-retrieval long-term memory.

Mirrors jiuwenswarm's MemoryIndexManager (6 tables, hybrid vector+FTS, mtime
incremental indexing, embedding cache) but slimmed for Twinkle. Vectors are
opt-in (sqlite-vec optional extra); without it or without an embedding API key,
search degrades to FTS-only. All public methods return strings or lists — never
raise on bad input (errors go through returned strings, like skill_tools).
"""
from __future__ import annotations

import datetime
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("twinkle.memory")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_ROOT_FILES = ("USER.md", "MEMORY.md")


class MemoryManager:
    def __init__(
        self,
        memory_dir: str,
        embed_provider=None,
        *,
        dims: int = 1536,
        chunk_tokens: int = 256,
        chunk_overlap: int = 32,
        max_results: int = 10,
        min_score: float = 0.3,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
        candidate_multiplier: float = 2.0,
        max_chunks_per_file: int = 200,
    ) -> None:
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "daily_memory").mkdir(exist_ok=True)
        self._provider = embed_provider
        self._dims = dims
        self._chunk_tokens = chunk_tokens
        self._chunk_overlap = chunk_overlap
        self._max_results = max_results
        self._min_score = min_score
        self._vw = vector_weight
        self._tw = text_weight
        self._cand_mult = candidate_multiplier
        self._max_chunks = max_chunks_per_file
        self._db = sqlite3.connect(str(self._dir / "memory.db"))
        self._db.row_factory = sqlite3.Row
        self._vec_enabled = False
        self._ensure_schema()
        self._check_model_changed()

    # --- schema -----------------------------------------------------------
    def _ensure_schema(self) -> None:
        db = self._db
        db.execute(
            "CREATE TABLE IF NOT EXISTS chunks("
            "id TEXT PRIMARY KEY, path TEXT, source TEXT, start_line INTEGER,"
            "end_line INTEGER, hash TEXT, model TEXT, text TEXT, embedding BLOB,"
            "updated_at TEXT)")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS embedding_cache("
            "hash TEXT PRIMARY KEY, embedding BLOB, dims INTEGER, updated_at TEXT)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS files("
            "path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL, size INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        try:
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
                f"USING vec0(embedding float[{self._dims}] distance=cosine)")
            self._vec_enabled = True
        except Exception:
            log.warning("sqlite-vec unavailable; memory degrades to FTS-only")
        db.commit()

    # --- path validation --------------------------------------------------
    def _validate_path(self, path: str) -> str | None:
        """Whitelist: USER.md / MEMORY.md (root) or daily_memory/YYYY-MM-DD.md.
        Returns the resolved-relative path, or None if invalid."""
        if path in _ROOT_FILES:
            rel = path
        elif path.startswith("daily_memory/"):
            tail = path[len("daily_memory/"):]
            if not _DATE_RE.match(tail):
                return None
            rel = path
        else:
            return None
        try:
            resolved = (self._dir / rel).resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self._dir):
            return None
        return rel

    # --- listing / reading -----------------------------------------------
    def list_files(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self._dir.rglob("*.md")):
            if p.is_file():
                out.append(p.relative_to(self._dir).as_posix())
        return out

    def read(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        rel = self._validate_path(path)
        if rel is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / rel
        if not fpath.is_file():
            return f"Error: '{path}' not found."
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"Error reading '{path}': {exc}"
        lines = text.splitlines()
        start = offset or 0
        end = None if limit is None else start + limit
        return "\n".join(lines[start:end])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "memory: MemoryManager schema + path whitelist + list/read"
```

---

## Task 3: MemoryManager — write + FTS index + FTS-only search

**Files:**
- Modify: `twinkle/agentserver/memory/store.py` (add `write`, `_index_file`, `_chunk`, `search`, helpers)
- Test: `tests/test_memory_store.py` (append)

- [ ] **Step 1: Append failing tests**

```python
# append to tests/test_memory_store.py
def test_write_then_read_back(tmp_path):
    mgr = _mgr(tmp_path)
    out = mgr.write("MEMORY.md", "项目使用 Python 3.12", append=True)
    assert "Stored" in out
    assert "项目使用 Python 3.12" in mgr.read("MEMORY.md")


def test_write_append_adds_newline(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "first", append=True)
    mgr.write("MEMORY.md", "second", append=True)
    assert mgr.read("MEMORY.md") == "first\nsecond"


def test_write_overwrite(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "old", append=True)
    mgr.write("MEMORY.md", "new", append=False)
    assert mgr.read("MEMORY.md") == "new"


def test_write_invalid_path(tmp_path):
    out = _mgr(tmp_path).write("../escape.md", "x", append=True)
    assert "invalid" in out.lower()


def test_write_creates_daily_subdir(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-07-27.md", "today: shipped fix", append=True)
    assert (tmp_path / "daily_memory" / "2026-07-27.md").is_file()


def test_search_fts_only_hits_written_fact(tmp_path):
    """No embed_provider → FTS-only; write a fact, search by keyword, hit."""
    mgr = _mgr(tmp_path)  # embed_provider=None
    mgr.write("MEMORY.md", "用户偏好用中文回答问题。", append=True)
    mgr.write("MEMORY.md", "项目架构是两进程 WebSocket。", append=True)
    hits = mgr.search("偏好")
    assert hits
    assert any("偏好" in h["text"] for h in hits)


def test_search_fts_only_miss(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "项目用 Python 3.12", append=True)
    assert mgr.search("completelyunrelatedterm") == []


def test_search_logs(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="twinkle.memory")
    _mgr(tmp_path).search("x")
    assert any("memory_search" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: FAIL — `MemoryManager` has no `write`/`search` attributes.

- [ ] **Step 3: Add write + index + search methods to `MemoryManager`**

Insert these methods into the `MemoryManager` class in `store.py` (after `read`):

```python
    # --- writing + indexing ----------------------------------------------
    def write(self, path: str, content: str, append: bool = False) -> str:
        rel = self._validate_path(path)
        if rel is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with fpath.open("a" if append else "w", encoding="utf-8") as f:
                f.write(content)
                if append and not content.endswith("\n"):
                    f.write("\n")
        except OSError as exc:
            return f"Error writing '{path}': {exc}"
        self._index_file(rel)
        log.info("write_memory path=%s append=%s", rel, append)
        return f"Stored to {rel}."

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        rel = self._validate_path(path)
        if rel is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / rel
        if not fpath.is_file():
            return f"Error: '{path}' not found."
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"Error reading '{path}': {exc}"
        if old_text not in text:
            return f"Error: old_text not found in '{path}'."
        fpath.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        self._index_file(rel)
        log.info("edit_memory path=%s", rel)
        return f"Edited {rel}."

    def _index_file(self, rel: str) -> None:
        import hashlib
        fpath = self._dir / rel
        try:
            stat = fpath.stat()
            content = fpath.read_text(encoding="utf-8")
        except OSError:
            return
        file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        row = self._db.execute(
            "SELECT mtime, size, hash FROM files WHERE path=?", (rel,)).fetchone()
        if (row and row["mtime"] == stat.st_mtime
                and row["size"] == stat.st_size and row["hash"] == file_hash):
            return  # unchanged — skip re-index

        # delete old chunks for this file (collect rowids first)
        old = [r["rowid"] for r in self._db.execute(
            "SELECT rowid FROM chunks WHERE path=?", (rel,)).fetchall()]
        if old:
            ph = ",".join("?" * len(old))
            self._db.execute("DELETE FROM chunks WHERE path=?", (rel,))
            self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({ph})", old)
            if self._vec_enabled:
                self._db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({ph})", old)

        want_vec = self._vec_enabled and self._provider is not None
        chunks = self._chunk(content)
        texts = [c[2] for c in chunks]
        embeddings = self._embed_chunks(texts) if want_vec else [None] * len(chunks)
        now = datetime.datetime.now().isoformat()
        model = self._provider.model if self._provider else ""
        for (start, end, text), emb in zip(chunks, embeddings):
            cid = f"{rel}:{start}:{end}"
            blob = self._serialize(emb) if emb is not None else None
            cur = self._db.execute(
                "INSERT INTO chunks(id,path,source,start_line,end_line,hash,model,"
                "text,embedding,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid, rel, "memory", start, end, file_hash, model, text, blob, now))
            rowid = cur.lastrowid
            self._db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                             (rowid, text))
            if want_vec and blob is not None:
                self._db.execute(
                    "INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)",
                    (rowid, blob))
        self._db.execute(
            "INSERT OR REPLACE INTO files(path,source,hash,mtime,size) VALUES(?,?,?,?,?)",
            (rel, "memory", file_hash, stat.st_mtime, stat.st_size))
        self._db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('embed_model', ?)",
            (model,))
        self._enforce_cap(rel)
        self._db.commit()

    def _chunk(self, content: str) -> list[tuple[int, int, str]]:
        lines = content.splitlines()
        if not lines:
            return []
        budget = max(1, self._chunk_tokens) * 3
        overlap = max(0, self._chunk_overlap) * 3
        out: list[tuple[int, int, str]] = []
        i, n = 0, len(lines)
        while i < n:
            size, j = 0, i
            while j < n and size < budget:
                size += len(lines[j]) + 1
                j += 1
            out.append((i + 1, j, "\n".join(lines[i:j])))
            if j >= n:
                break
            back, k = 0, j
            while k > i + 1 and back < overlap:
                k -= 1
                back += len(lines[k]) + 1
            i = k if k > i else j
        return out

    def _embed_chunks(self, texts: list[str]) -> list:
        import hashlib
        import sqlite3 as _sqlite
        out: list = []
        to_embed: list[tuple[str, str]] = []
        cached: dict[str, bytes] = {}
        for t in texts:
            h = hashlib.md5(t.encode("utf-8")).hexdigest()
            row = self._db.execute(
                "SELECT embedding FROM embedding_cache WHERE hash=?", (h,)).fetchone()
            if row and row["embedding"]:
                cached[h] = row["embedding"]
            else:
                to_embed.append((h, t))
        new: dict[str, bytes] = {}
        if to_embed:
            try:
                vecs = self._provider.embed([t for _, t in to_embed])
                now = datetime.datetime.now().isoformat()
                for (h, _), vec in zip(to_embed, vecs):
                    blob = self._serialize(vec)
                    self._db.execute(
                        "INSERT OR REPLACE INTO embedding_cache"
                        "(hash,embedding,dims,updated_at) VALUES(?,?,?,?)",
                        (h, blob, self._dims, now))
                    new[h] = blob
            except Exception as exc:
                log.warning("embedding failed, chunks left un-indexed for retry: %s", exc)
        for t in texts:
            h = hashlib.md5(t.encode("utf-8")).hexdigest()
            out.append(new.get(h, cached.get(h)))
        return out

    @staticmethod
    def _serialize(vec) -> bytes:
        try:
            from sqlite_vec import serialize_floats
            return serialize_floats(vec)
        except Exception:
            import struct
            return struct.pack(f"{len(vec)}f", *vec)

    def _enforce_cap(self, rel: str) -> None:
        count = self._db.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        if count <= self._max_chunks:
            return
        excess = count - self._max_chunks
        old = [r["rowid"] for r in self._db.execute(
            "SELECT rowid FROM chunks WHERE path=? ORDER BY updated_at ASC LIMIT ?",
            (rel, excess)).fetchall()]
        if not old:
            return
        ph = ",".join("?" * len(old))
        self._db.execute(f"DELETE FROM chunks WHERE rowid IN ({ph})", old)
        self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({ph})", old)
        if self._vec_enabled:
            self._db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({ph})", old)

    def _check_model_changed(self) -> None:
        if self._provider is None:
            return
        row = self._db.execute(
            "SELECT value FROM meta WHERE key='embed_model'").fetchone()
        if row and row["value"] and row["value"] != self._provider.model:
            log.warning("embed model %r -> %r, rebuilding index",
                        row["value"], self._provider.model)
            self._db.execute("DELETE FROM chunks")
            self._db.execute("DELETE FROM chunks_fts")
            if self._vec_enabled:
                self._db.execute("DELETE FROM chunks_vec")
            self._db.execute("DELETE FROM embedding_cache")
            self._db.execute("DELETE FROM files")
            self._db.commit()

    # --- search ----------------------------------------------------------
    def search(self, query: str, max_results: int | None = None,
               min_score: float | None = None) -> list[dict]:
        max_results = max_results or self._max_results
        min_score = self._min_score if min_score is None else min_score
        candidates = min(200, max(1, int(max_results * self._cand_mult)))
        fts = self._fts_search(query, candidates)
        hybrid = self._vec_enabled and self._provider is not None
        if not hybrid:
            results = [(r["rowid"], r, self._text_sim(r["bm"])) for r in fts]
        else:
            vec = self._vec_search(query, candidates)
            chunk_map = {r["rowid"]: r for r in fts}
            for rid, vsim in vec.items():
                if rid not in chunk_map:
                    cr = self._db.execute(
                        "SELECT rowid, path, text, start_line, end_line "
                        "FROM chunks WHERE rowid=?", (rid,)).fetchone()
                    if cr:
                        chunk_map[rid] = cr
            results = []
            for rid, chunk in chunk_map.items():
                t = self._text_sim(_fts_bm(rid)) if False else 0.0
                # FTS bm only exists for rows fts matched; use map if present
            # recompute properly:
            fts_bm = {r["rowid"]: r["bm"] for r in fts}
            results = []
            all_ids = set(fts_bm) | set(vec)
            for rid in all_ids:
                t = self._text_sim(fts_bm.get(rid, 0.0)) if rid in fts_bm else 0.0
                v = vec.get(rid, 0.0)
                results.append((rid, chunk_map.get(rid),
                                self._vw * v + self._tw * t))
        results = [(r, s) for rid, r, s in results if r is not None and s >= min_score]
        results.sort(key=lambda x: x[1], reverse=True)
        results = results[:max_results]
        log.info("memory_search query=%r hits=%d", query, len(results))
        return [{"path": dict(r)["path"], "score": round(s, 4),
                 "text": dict(r)["text"]} for r, s in results]

    def _fts_search(self, query: str, limit: int):
        phrase = '"' + query.replace('"', '""') + '"'
        return self._db.execute(
            "SELECT c.rowid, c.path, c.text, c.start_line, c.end_line, "
            "bm25(chunks_fts) AS bm FROM chunks_fts JOIN chunks c "
            "ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? "
            "ORDER BY bm LIMIT ?",
            (phrase, limit)).fetchall()

    def _vec_search(self, query: str, limit: int) -> dict[int, float]:
        try:
            qvec = self._provider.embed([query])[0]
        except Exception as exc:
            log.warning("query embedding failed, vector leg skipped: %s", exc)
            return {}
        rows = self._db.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?", (self._serialize(qvec), limit)).fetchall()
        # cosine distance ∈ [0,2] → similarity = 1 - distance/2, clamped [0,1]
        return {r["rowid"]: max(0.0, 1.0 - r["distance"] / 2.0) for r in rows}

    @staticmethod
    def _text_sim(bm: float) -> float:
        return 1.0 / (1.0 + abs(bm))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS (all FTS-only tests; hybrid path not exercised here — Task 4).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "memory: write + mtime-incremental indexing + FTS-only search"
```

---

## Task 4: MemoryManager — hybrid search + degradation (sqlite-vec)

**Files:**
- Modify: `twinkle/agentserver/memory/store.py` (the `search` body already supports hybrid — this task adds the test + cleans the placeholder)
- Test: `tests/test_memory_store.py` (append)

- [ ] **Step 1: Simplify the `search` method**

The `search` written in Task 3 has leftover scratch lines (`t = self._text_sim(_fts_bm(rid)) if False else 0.0` and a redundant recompute). Replace the whole `search` method with this clean version:

```python
    def search(self, query: str, max_results: int | None = None,
               min_score: float | None = None) -> list[dict]:
        max_results = max_results or self._max_results
        min_score = self._min_score if min_score is None else min_score
        candidates = min(200, max(1, int(max_results * self._cand_mult)))
        fts = self._fts_search(query, candidates)
        fts_bm = {r["rowid"]: r for r in fts}
        hybrid = self._vec_enabled and self._provider is not None
        vec: dict[int, float] = self._vec_search(query, candidates) if hybrid else {}
        chunk_map = dict(fts_bm)
        for rid in vec:
            if rid not in chunk_map:
                cr = self._db.execute(
                    "SELECT rowid, path, text, start_line, end_line "
                    "FROM chunks WHERE rowid=?", (rid,)).fetchone()
                if cr:
                    chunk_map[rid] = cr
        results: list[tuple] = []
        for rid, chunk in chunk_map.items():
            t = self._text_sim(chunk["bm"]) if rid in fts_bm else 0.0
            v = vec.get(rid, 0.0)
            score = (self._vw * v + self._tw * t) if hybrid else t
            results.append((rid, chunk, score))
        results = [(r, s) for _rid, r, s in results if r is not None and s >= min_score]
        results.sort(key=lambda x: x[1], reverse=True)
        results = results[:max_results]
        log.info("memory_search query=%r hits=%d", query, len(results))
        return [{"path": dict(r)["path"], "score": round(s, 4),
                 "text": dict(r)["text"]} for r, s in results]
```

Note: in FTS-only mode (`hybrid` False), `score = t` (text similarity only) and `vec` is empty — pure FTS ranking. With sqlite-vec + a provider, both legs fuse.

- [ ] **Step 2: Append hybrid test (skipped if sqlite-vec not installed)**

```python
# append to tests/test_memory_store.py
def test_hybrid_search_runs_with_sqlite_vec(tmp_path):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8),
                        dims=8)
    assert mgr._vec_enabled  # noqa: SLF001
    mgr.write("MEMORY.md", "用户偏好用中文回答问题。", append=True)
    mgr.write("MEMORY.md", "项目架构是两进程 WebSocket。", append=True)
    hits = mgr.search("偏好")
    # FTS leg guarantees the right chunk ranks; hybrid fusion doesn't break it
    assert any("偏好" in h["text"] for h in hits)


def test_mtv_degrades_to_fts_only_when_no_provider(tmp_path):
    """sqlite-vec installed but no provider (no API key) -> FTS-only, no vector leg."""
    pytest.importorskip("sqlite_vec")
    mgr = MemoryManager(str(tmp_path), embed_provider=None)  # no provider
    assert mgr._vec_enabled  # noqa: SLF001 — extension loaded
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    hits = mgr.search("偏好")
    assert any("偏好" in h["text"] for h in hits)  # FTS still works
```

- [ ] **Step 3: Install the `[memory]` extra for local test runs**

Run: `.venv/Scripts/python.exe -m pip install sqlite-vec`
Expected: `Successfully installed sqlite-vec-*`

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS (hybrid tests run since sqlite-vec now installed; FTS-only tests still pass).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory/store.py tests/test_memory_store.py
git commit -m "memory: hybrid vector+FTS search with FTS-only degradation"
```

---

## Task 5: MemoryManager — edit + model-change rebuild + FIFO cap

**Files:**
- Modify: `tests/test_memory_store.py` (append) — `edit` method already exists from Task 3; this task tests it + rebuild + cap.

- [ ] **Step 1: Append failing tests**

```python
# append to tests/test_memory_store.py
def test_edit_replaces_and_reindexes(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好英文。", append=True)
    mgr.edit("MEMORY.md", "英文", "中文")
    assert "用户偏好中文。" in mgr.read("MEMORY.md")
    assert "英文" not in mgr.read("MEMORY.md")
    # old text no longer retrievable, new text is
    assert any("中文" in h["text"] for h in mgr.search("偏好"))
    assert not any("英文" in h["text"] for h in mgr.search("偏好"))


def test_edit_old_text_missing(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "hello", append=True)
    out = mgr.edit("MEMORY.md", "nope", "x")
    assert "not found" in out.lower()


def test_model_change_rebuilds_index(tmp_path):
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8, model="v1"),
                        dims=8)
    mgr.write("MEMORY.md", "some fact", append=True)
    assert mgr._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    # swap provider to a different model name -> rebuild
    mgr._provider = MockEmbeddingProvider(dims=8, model="v2")
    mgr._check_model_changed()
    assert mgr._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert mgr._db.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone() is None


def test_fifo_cap_evicts_oldest(tmp_path):
    mgr = MemoryManager(str(tmp_path), embed_provider=None, max_chunks_per_file=2)
    # each write re-indexes (overwrites chunks for the file); to exceed the cap
    # we need >2 chunks in one file. Write a long content with 3+ chunks.
    long_content = "\n".join(f"line {i} has unique content number {i}" for i in range(20))
    mgr.write("MEMORY.md", long_content, append=False)
    count = mgr._db.execute("SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0]
    assert count <= 2  # capped
```

- [ ] **Step 2: Run tests to verify they pass (edit/rebuild/cap already implemented in Task 3)**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS — `edit`, `_check_model_changed`, `_enforce_cap` were implemented in Task 3; these tests pin their behavior.

> If `test_model_change_rebuilds_index` fails because `_check_model_changed` only runs in `__init__`: it's also callable directly (Task 3 defined it as a public-ish method). The test calls it explicitly, which is the intended escape hatch for a model swap at runtime.

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory_store.py
git commit -m "memory: tests for edit / model-change rebuild / FIFO cap"
```

---

## Task 6: `memory/__init__.py` singleton

**Files:**
- Create: `twinkle/agentserver/memory/__init__.py`
- Test: `tests/test_memory_store.py` (append)

- [ ] **Step 1: Append failing test**

```python
# append to tests/test_memory_store.py
def test_get_memory_manager_singleton(tmp_path, monkeypatch):
    from twinkle.agentserver.memory import get_memory_manager, _set_memory_manager
    mgr = MemoryManager(str(tmp_path), embed_provider=None)
    _set_memory_manager(mgr)
    try:
        a = get_memory_manager()
        b = get_memory_manager()
        assert a is b
    finally:
        _set_memory_manager(None)


def test_set_memory_manager_reset(tmp_path):
    import twinkle.agentserver.memory as m
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    _set_memory_manager(None)
    assert m._MEMORY_MANAGER is None  # noqa: SLF001
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: FAIL — `get_memory_manager` not found (no `__init__.py` yet).

- [ ] **Step 3: Write `__init__.py`**

```python
# twinkle/agentserver/memory/__init__.py
"""memory package — re-exports + process-level singleton (mirrors skills/__init__)."""
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.memory.embeddings import (
    EmbeddingProvider,
    MockEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)

_MEMORY_MANAGER: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    """Process singleton (lazy). @tool funcs + MemoryHook call this; tests swap
    via _set_memory_manager. lazy import config avoids import-time side effects."""
    global _MEMORY_MANAGER
    if _MEMORY_MANAGER is None:
        from twinkle.config import (
            LLM_API_KEY, LLM_BASE_URL,
            MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
            MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE,
            MEMORY_DIR, MEMORY_EMBED_MODEL,
            MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
            MEMORY_QUERY_MAX_RESULTS, MEMORY_QUERY_MIN_SCORE,
        )
        provider = None
        dims = 1536  # matches text-embedding-3-small; change model + dims -> delete memory.db
        if LLM_API_KEY:
            provider = OpenAICompatibleEmbeddingProvider(
                LLM_BASE_URL, LLM_API_KEY, MEMORY_EMBED_MODEL, dims)
        _MEMORY_MANAGER = MemoryManager(
            MEMORY_DIR, provider, dims=dims,
            chunk_tokens=MEMORY_CHUNKING_TOKENS,
            chunk_overlap=MEMORY_CHUNKING_OVERLAP,
            max_results=MEMORY_QUERY_MAX_RESULTS,
            min_score=MEMORY_QUERY_MIN_SCORE,
            vector_weight=MEMORY_HYBRID_VECTOR_WEIGHT,
            text_weight=MEMORY_HYBRID_TEXT_WEIGHT,
            candidate_multiplier=MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            max_chunks_per_file=MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE)
    return _MEMORY_MANAGER


def _set_memory_manager(mgr: MemoryManager | None) -> None:
    """Test hook: replace/reset the singleton. Production code never calls this."""
    global _MEMORY_MANAGER
    _MEMORY_MANAGER = mgr


__all__ = [
    "MemoryManager", "EmbeddingProvider", "MockEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "get_memory_manager", "_set_memory_manager",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS (singleton tests). Note: this requires the config constants from Task 9 — if run before Task 9, `get_memory_manager()` will fail at the config import *only when called without a pre-set singleton*. The singleton tests set the singleton first, so the config import path isn't hit. Good.

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/memory/__init__.py tests/test_memory_store.py
git commit -m "memory: get_memory_manager singleton + _set test hook"
```

---

## Task 7: 4 `@tool`s + registration

**Files:**
- Create: `twinkle/agentserver/tools/builtin/memory_tools.py`
- Modify: `twinkle/agentserver/tools/__init__.py`
- Test: `tests/test_memory_tools.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_tools.py
import asyncio
import pytest
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.tools.manager import ToolManager


@pytest.fixture
def isolated_memory(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


def _names(tm):
    return sorted(c.name for c in tm._tools.values())  # noqa: SLF001


def test_four_tools_registered():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    for n in ("memory_search", "write_memory", "read_memory", "edit_memory"):
        assert n in _names(tm), f"{n} not registered"


def test_write_read_search_round_trip(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import (
        memory_search, read_memory, write_memory)
    out = asyncio.run(write_memory.func("MEMORY.md", "用户偏好中文。", True))
    assert "Stored" in out
    body = asyncio.run(read_memory.func("MEMORY.md"))
    assert "用户偏好中文。" in body
    hits = asyncio.run(memory_search.func("偏好"))
    assert any("偏好" in h for h in [hits])  # search returns a formatted string
    assert "偏好" in hits


def test_edit_tool(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import edit_memory, write_memory
    asyncio.run(write_memory.func("MEMORY.md", "偏好英文。", True))
    out = asyncio.run(edit_memory.func("MEMORY.md", "英文", "中文"))
    assert "Edited" in out


def test_tool_returns_error_string_on_bad_path(isolated_memory):
    from twinkle.agentserver.tools.builtin.memory_tools import write_memory
    out = asyncio.run(write_memory.func("../escape.md", "x", True))
    assert "invalid" in out.lower()  # no raise, no ReAct crash


def test_schemas_expose_params():
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    schemas = {c.name: c.parameters for c in tm._tools.values()}  # noqa: SLF001
    assert schemas["memory_search"]["properties"]["query"]["type"] == "string"
    assert "max_results" in schemas["memory_search"]["properties"]
    assert schemas["write_memory"]["properties"]["append"]["type"] == "boolean"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_tools.py -v`
Expected: FAIL — `memory_tools` module doesn't exist.

- [ ] **Step 3: Write the 4 `@tool`s**

```python
# twinkle/agentserver/tools/builtin/memory_tools.py
"""Memory tools — model-driven read/write/search/edit over long-term memory.

Thin wrappers around get_memory_manager(), mirroring skill_tools. All return
strings; errors are returned (never raised) so a bad call doesn't crash ReAct.
"""
from __future__ import annotations

from twinkle.agentserver.memory import get_memory_manager
from twinkle.agentserver.tools.decorator import tool


@tool
async def memory_search(query: str, max_results: int | None = None,
                        min_score: float | None = None) -> str:
    """Search long-term memory for relevant facts. Call when the answer depends on
    cross-session user preferences, history, or past decisions."""
    hits = get_memory_manager().search(query, max_results=max_results, min_score=min_score)
    if not hits:
        return "No relevant memories found."
    lines = [f"## 记忆召回 ({len(hits)} 条)"]
    for h in hits:
        lines.append(f"### {h['path']} (score {h['score']})\n{h['text']}")
    return "\n\n".join(lines)


@tool
async def write_memory(path: str, content: str, append: bool = False) -> str:
    """Write a fact to long-term memory. path: USER.md (user profile), MEMORY.md
    (decisions/preferences/persistent facts), or daily_memory/YYYY-MM-DD.md (daily
    notes / when the user says 'remember this')."""
    return get_memory_manager().write(path, content, append=append)


@tool
async def read_memory(path: str, offset: int | None = None,
                      limit: int | None = None) -> str:
    """Read a memory file's contents (line-based offset/limit paging)."""
    return get_memory_manager().read(path, offset=offset, limit=limit)


@tool
async def edit_memory(path: str, old_text: str, new_text: str) -> str:
    """Edit a memory file by replacing old_text with new_text. Use to correct
    stale or contradicted memories."""
    return get_memory_manager().edit(path, old_text, new_text)
```

- [ ] **Step 4: Register the 4 tools in `tool_manager()`**

Modify `twinkle/agentserver/tools/__init__.py`:
- Add to the import on line 11: include `memory_tools` in the `from ...builtin import ...` line.
- Register the four tools inside `tool_manager()`.

The import line (line 11) becomes:
```python
from twinkle.agentserver.tools.builtin import command_exec, file_tools, memory_tools, skill_tools, todo_tools, web_fetch, web_search
```
And inside `tool_manager()` (after `tm.register(skill_tools.read_skill)`), add:
```python
    tm.register(memory_tools.memory_search)
    tm.register(memory_tools.write_memory)
    tm.register(memory_tools.read_memory)
    tm.register(memory_tools.edit_memory)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_tools.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/tools/builtin/memory_tools.py twinkle/agentserver/tools/__init__.py tests/test_memory_tools.py
git commit -m "memory: 4 @tool functions + register in tool_manager"
```

---

## Task 8: `MemoryHook` + registration

**Files:**
- Create: `twinkle/agentserver/hooks/builtin/memory_hook.py`
- Modify: `twinkle/agentserver/hooks/builtin/__init__.py`
- Test: `tests/test_memory_hook.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_hook.py
import asyncio
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager


def _ctx(messages=None):
    return HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                       inputs=ModelCallInputs(messages=messages or [], tools=[]),
                       session_id="s", request_id="r")


@pytest.fixture
def empty_memory(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


@pytest.fixture
def populated_memory(tmp_path):
    mgr = MemoryManager(str(tmp_path), embed_provider=None)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    _set_memory_manager(mgr)
    yield tmp_path
    _set_memory_manager(None)


def test_noop_when_no_memory(empty_memory):
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]


def test_injects_prompt_when_memory_present(populated_memory):
    ctx = _ctx([{"role": "user", "content": "hi"}])
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages[0]["role"] == "system"
    body = ctx.inputs.messages[0]["content"]
    assert "memory_search" in body
    assert "write_memory" in body
    assert "USER.md" in body and "MEMORY.md" in body
    # today's daily path substituted
    import datetime
    assert f"daily_memory/{datetime.date.today().isoformat()}.md" in body
    # original message preserved after
    assert ctx.inputs.messages[1] == {"role": "user", "content": "hi"}


def test_replaces_list_not_mutate(populated_memory):
    original = [{"role": "user", "content": "hi"}]
    ctx = _ctx(original)
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert original == [{"role": "user", "content": "hi"}]  # not mutated in place
    assert ctx.inputs.messages is not original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: FAIL — `memory_hook` module doesn't exist.

- [ ] **Step 3: Write `MemoryHook`**

```python
# twinkle/agentserver/hooks/builtin/memory_hook.py
"""MemoryHook — before_model_call injects the long-term-memory usage-strategy prompt.

No-op when the memory store is empty. Injects by assigning a NEW list (never
mutates the store's internal list in place), mirroring SkillHook. 5a ships only
the 'proactive' prompt; a 'passive' variant is a future easy-add (config + a
second prompt string, no code-logic change).
"""
from __future__ import annotations

import datetime

from twinkle.agentserver.hooks.base import AgentHook, HookContext

_PROMPT_TEMPLATE = """## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 {mem_dir}。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/{today}.md", ...)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。"""


class MemoryHook(AgentHook):
    priority = 80  # functional layer (50-99); below SkillHook(90)

    async def before_model_call(self, ctx: HookContext) -> None:
        from twinkle.agentserver.memory import get_memory_manager
        if not get_memory_manager().list_files():
            return  # empty store → no-op
        self._prepend(ctx, _build_prompt())

    @staticmethod
    def _prepend(ctx: HookContext, content: str) -> None:
        # assign a new list — msgs may be the store's internal list
        ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages


def _build_prompt() -> str:
    from twinkle.config import MEMORY_DIR
    return _PROMPT_TEMPLATE.format(
        mem_dir=MEMORY_DIR, today=datetime.date.today().isoformat())
```

- [ ] **Step 4: Export `MemoryHook` from `hooks/builtin/__init__.py`**

Modify `twinkle/agentserver/hooks/builtin/__init__.py` — add the import + `__all__` entry:

```python
from twinkle.agentserver.hooks.builtin.logging_hook import LoggingHook
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.hooks.builtin.permission_hook import PermissionHook
from twinkle.agentserver.hooks.builtin.skill_hook import SkillHook

__all__ = ["LoggingHook", "MemoryHook", "PermissionHook", "SkillHook"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_hook.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/hooks/builtin/memory_hook.py twinkle/agentserver/hooks/builtin/__init__.py tests/test_memory_hook.py
git commit -m "memory: MemoryHook (before_model_call usage-strategy prompt) + export"
```

---

## Task 9: Config — `memory:` block + permissions + flatten

**Files:**
- Modify: `twinkle/config/schema.py`
- Modify: `twinkle/config/__init__.py`
- Modify: `twinkle/resources/config.yaml`
- Test: `tests/test_memory_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_config.py
from twinkle.config import (
    MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
    MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE, MEMORY_DIR,
    MEMORY_EMBED_MODEL, MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
    MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
    MEMORY_QUERY_MAX_RESULTS, MEMORY_QUERY_MIN_SCORE,
)
from twinkle.config import settings


def test_memory_constants_flattened():
    assert MEMORY_EMBED_MODEL == "text-embedding-3-small"
    assert MEMORY_QUERY_MAX_RESULTS == 10
    assert abs(MIN_SCORE := MEMORY_QUERY_MIN_SCORE - 0.3) < 1e-9 or True
    assert MEMORY_QUERY_MIN_SCORE == 0.3
    assert MEMORY_HYBRID_VECTOR_WEIGHT == 0.7
    assert MEMORY_HYBRID_TEXT_WEIGHT == 0.3
    assert MEMORY_HYBRID_CANDIDATE_MULTIPLIER == 2.0
    assert MEMORY_CHUNKING_TOKENS == 256
    assert MEMORY_CHUNKING_OVERLAP == 32
    assert MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE == 200
    assert MEMORY_DIR.endswith(".twinkle_data/memory")


def test_memory_tools_in_permissions_defaults():
    tools = settings.permissions.tools
    for t in ("memory_search", "write_memory", "read_memory", "edit_memory"):
        assert tools.get(t) == "allow", f"{t} not allow in default permissions"


def test_strict_model_rejects_unknown_memory_key():
    import pytest
    from pydantic import ValidationError
    from twinkle.config.schema import MemoryConfig
    with pytest.raises(ValidationError):
        MemoryConfig(embed_model="x", totallyboguskey=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_config.py -v`
Expected: FAIL — `MEMORY_*` constants + `MemoryConfig` don't exist.

- [ ] **Step 3: Add nested `MemoryConfig` models to `schema.py`**

In `twinkle/config/schema.py`, add these classes (after `SkillsConfig`, before `PermissionsConfig`):

```python
class MemoryQueryConfig(_StrictModel):
    max_results: int = 10
    min_score: float = 0.3


class MemoryHybridConfig(_StrictModel):
    vector_weight: float = 0.7
    text_weight: float = 0.3
    candidate_multiplier: float = 2.0


class MemoryChunkingConfig(_StrictModel):
    tokens: int = 256
    overlap: int = 32


class MemoryCleanupConfig(_StrictModel):
    max_chunks_per_file: int = 200


class MemoryConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/.twinkle_data/memory
    embed_model: str = "text-embedding-3-small"
    query: MemoryQueryConfig = MemoryQueryConfig()
    hybrid: MemoryHybridConfig = MemoryHybridConfig()
    chunking: MemoryChunkingConfig = MemoryChunkingConfig()
    cleanup: MemoryCleanupConfig = MemoryCleanupConfig()
```

Add the 4 memory tools to the `PermissionsConfig.tools` default dict:

```python
    tools: dict[str, PermissionTier] = {
        "command_exec": "require-approval",
        "web_fetch": "allow",
        "web_search": "allow",
        "todo_create": "allow",
        "todo_complete": "allow",
        "todo_list": "allow",
        "memory_search": "allow",
        "write_memory": "allow",
        "read_memory": "allow",
        "edit_memory": "allow",
    }
```

Add `memory` to `TwinkleConfig` (after `skills`):

```python
    skills: SkillsConfig = SkillsConfig()
    memory: MemoryConfig = MemoryConfig()
    permissions: PermissionsConfig = PermissionsConfig()
```

Add MEMORY_DIR derivation in `_derive_paths` (after the skills.dir block):

```python
        if not self.skills.dir:
            self.skills.dir = str(Path(ws) / "skills")
        else:
            self.skills.dir = os.path.expanduser(self.skills.dir)
        if not self.memory.dir:
            self.memory.dir = str(Path(ws) / ".twinkle_data" / "memory")
        else:
            self.memory.dir = os.path.expanduser(self.memory.dir)
```

- [ ] **Step 4: Add the `memory:` block to `config.yaml`**

In `twinkle/resources/config.yaml`, after the `skills:` block and before `permissions:`, insert:

```yaml
memory:
  dir: ${TWINKLE_MEMORY_DIR:-}        # 空 → <workspace>/.twinkle_data/memory
  embed_model: text-embedding-3-small # 复用 llm.api_key + llm.base_url,不另开 embed.* env
  query:
    max_results: 10
    min_score: 0.3
  hybrid:
    vector_weight: 0.7
    text_weight: 0.3
    candidate_multiplier: 2.0
  chunking:
    tokens: 256
    overlap: 32
  cleanup:
    max_chunks_per_file: 200          # 单文件 chunk 上限,超限 FIFO 丢最旧(5a 唯一自动淘汰;5c Dreaming 取代)
```

And in the `permissions.tools:` block, add:

```yaml
    memory_search: allow
    write_memory: allow
    read_memory: allow
    edit_memory: allow
```

- [ ] **Step 5: Flatten constants in `config/__init__.py`**

In `twinkle/config/__init__.py`, after the `# --- skills (Phase 7) ---` block, add:

```python
# --- memory (Phase 5a) ---
MEMORY_DIR = settings.memory.dir
MEMORY_EMBED_MODEL = settings.memory.embed_model
MEMORY_QUERY_MAX_RESULTS = settings.memory.query.max_results
MEMORY_QUERY_MIN_SCORE = settings.memory.query.min_score
MEMORY_HYBRID_VECTOR_WEIGHT = settings.memory.hybrid.vector_weight
MEMORY_HYBRID_TEXT_WEIGHT = settings.memory.hybrid.text_weight
MEMORY_HYBRID_CANDIDATE_MULTIPLIER = settings.memory.hybrid.candidate_multiplier
MEMORY_CHUNKING_TOKENS = settings.memory.chunking.tokens
MEMORY_CHUNKING_OVERLAP = settings.memory.chunking.overlap
MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE = settings.memory.cleanup.max_chunks_per_file
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml tests/test_memory_config.py
git commit -m "config: memory block (nested) + permissions.tools memory + MEMORY_* flatten"
```

---

## Task 10: `ensure_workspace_dir` creates `MEMORY_DIR` + `daily_memory/`

**Files:**
- Modify: `twinkle/workspace.py`
- Test: `tests/test_workspace_memory.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_workspace_memory.py
import importlib


def test_ensure_workspace_creates_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("TWINKLE_MEMORY_DIR", str(tmp_path / "mem"))
    import twinkle.config as cfg
    importlib.reload(cfg)
    import twinkle.workspace as ws
    importlib.reload(ws)
    ws.ensure_workspace_dir()
    assert (tmp_path / "mem").is_dir()
    assert (tmp_path / "mem" / "daily_memory").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workspace_memory.py -v`
Expected: FAIL — `MEMORY_DIR` not created by `ensure_workspace_dir` (it only makes WORKSPACE_DIR + SKILLS_DIR).

- [ ] **Step 3: Add `MEMORY_DIR` creation to `ensure_workspace_dir`**

In `twinkle/workspace.py`, inside `ensure_workspace_dir()` (after `os.makedirs(_cfg.SKILLS_DIR, exist_ok=True)`), add:

```python
    os.makedirs(_cfg.MEMORY_DIR, exist_ok=True)
    os.makedirs(os.path.join(_cfg.MEMORY_DIR, "daily_memory"), exist_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workspace_memory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add twinkle/workspace.py tests/test_workspace_memory.py
git commit -m "workspace: ensure MEMORY_DIR + daily_memory/ on startup"
```

---

## Task 11: Wire-in — drop stub, add `MemoryHook` to loop, `[memory]` extra

**Files:**
- Modify: `twinkle/agentserver/agent_loop.py` (drop `memory` param + import + `recall()` call)
- Modify: `twinkle/agentserver/server.py` (drop `LongTermMemory`, add `MemoryHook` to hook list)
- Delete: `twinkle/agentserver/memory.py` (stub)
- Modify: `pyproject.toml` (`memory` extra)
- Test: `tests/test_memory_wiring.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_memory_wiring.py
import pytest


def test_agent_loop_has_no_memory_param():
    """The LongTermMemory stub param is gone — memory is now hook+tools driven."""
    import inspect
    from twinkle.agentserver.agent_loop import AgentLoop
    params = inspect.signature(AgentLoop.__init__).parameters
    assert "memory" not in params
    assert "llm" in params and "store" in params and "tools" in params


def test_build_agent_loop_no_memory_arg():
    from twinkle.agentserver.server import build_agent_loop
    import inspect
    params = inspect.signature(build_agent_loop).parameters
    assert "memory" not in params


def test_memory_stub_module_removed():
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module("twinkle.agentserver.memory")
    # but the package exists:
    import twinkle.agentserver.memory as mem  # noqa: F401 — package, not the old stub
    # (the line above would also raise if the package dir + __init__ weren't there;
    #  the prior `with pytest.raises(ImportError)` catches a *bare module* path.)


def test_memory_hook_in_default_loop_hooks():
    """build_agent_loop + main() register MemoryHook alongside SkillHook."""
    # main() builds hooks explicitly; we check MemoryHook is exported + instantiable.
    from twinkle.agentserver.hooks.builtin import MemoryHook, SkillHook
    assert MemoryHook.priority < SkillHook.priority  # 80 < 90
```

> Note on `test_memory_stub_module_removed`: the old `memory.py` and the new `memory/` package can't coexist (Python resolves `twinkle.agentserver.memory` to the package once `memory.py` is deleted). The test asserts the package resolves (no ImportError for `twinkle.agentserver.memory`) after the stub is deleted. Simplify the test body to:

```python
def test_memory_stub_replaced_by_package():
    import twinkle.agentserver.memory as mem  # the package (not the old stub)
    from twinkle.agentserver.memory.store import MemoryManager
    assert hasattr(mem, "get_memory_manager")
    assert MemoryManager is not None
```

(Replace the body of `test_memory_stub_module_removed` with the above; delete the `with pytest.raises(ImportError)` version — it was over-engineered.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_wiring.py -v`
Expected: FAIL — `AgentLoop.__init__` still has `memory` param; `memory.py` stub still imports `LongTermMemory`.

- [ ] **Step 3: Drop `memory` from `agent_loop.py`**

In `twinkle/agentserver/agent_loop.py`:
- Delete line 16: `from twinkle.agentserver.memory import LongTermMemory`
- In `AgentLoop.__init__` (lines 61-72), remove the `memory: LongTermMemory,` parameter (line 66) and the `self._memory = memory` line (line 71). The signature becomes:
```python
    def __init__(
        self,
        llm: LLMClient,
        store: SessionStore,
        tools: ToolManager,
    ) -> None:
        self._llm = llm
        self._session_store = store
        self._tool_manager = tools
        self._hook_manager = HookManager()
```
- Delete line 156: `        self._memory.recall(query)` (the discarded stub call). Leave the surrounding `await self._session_store.append(...)` and `query = ...` lines intact.

- [ ] **Step 4: Drop `LongTermMemory` from `server.py` + add `MemoryHook` to the hook list**

In `twinkle/agentserver/server.py`:
- Delete line 21: `from twinkle.agentserver.memory import LongTermMemory`
- In `build_agent_loop` (lines 60-68), delete line 63 `    memory = LongTermMemory()` and change line 64 to `    loop = AgentLoop(llm, store, tools)`:
```python
    if llm is None:
        llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
    tools = tool_manager()
    loop = AgentLoop(llm, store, tools)
    if hooks:
        for hook in hooks:
            loop.register_hook(hook)
    return loop
```
- In `main()` (line 145), the import becomes:
```python
    from twinkle.agentserver.hooks.builtin import MemoryHook, PermissionHook, LoggingHook, SkillHook
```
- In `main()` (line 151), add `MemoryHook()` to the hooks list:
```python
    loop = build_agent_loop(
        store,
        hooks=[PermissionHook(engine), SkillHook(), MemoryHook(), LoggingHook()],
    )
```

- [ ] **Step 5: Delete the stub**

```bash
git rm twinkle/agentserver/memory.py
```

- [ ] **Step 6: Add the `[memory]` extra to `pyproject.toml`**

In `pyproject.toml`, in the `[project.optional-dependencies]` table (after the `obs` block), add:

```toml
memory = ["sqlite-vec"]
```

- [ ] **Step 7: Run the full test suite to verify nothing broke**

Run: `python -m pytest tests/ -v`
Expected: PASS — existing tests (agent_loop, server, skill, todo, etc.) still pass; the stub removal doesn't break anything because no production code referenced `LongTermMemory.recall`'s return value.

> If an existing test constructs `AgentLoop(..., memory=...)` or `build_agent_loop(..., memory=...)`, update it to drop the `memory` arg (grep `AgentLoop(` and `build_agent_loop(` in `tests/`).

- [ ] **Step 8: Run wiring tests to verify they pass**

Run: `python -m pytest tests/test_memory_wiring.py tests/test_memory_store.py tests/test_memory_tools.py tests/test_memory_hook.py tests/test_memory_embeddings.py tests/test_memory_config.py -v`
Expected: PASS (all memory tests).

- [ ] **Step 9: Commit**

```bash
git add twinkle/agentserver/agent_loop.py twinkle/agentserver/server.py pyproject.toml tests/test_memory_wiring.py
git commit -m "memory: wire MemoryHook into loop, drop LongTermMemory stub, add [memory] extra"
```

---

## Task 12: Integration — cross-session recall via ToolManager + hook injection

**Files:**
- Test: `tests/test_memory_integration.py`

This is an end-to-end behavior test (no real LLM): the `memory_search` tool_result round-trips through `ToolManager.execute`, and `MemoryHook` injects on a populated store. The agent_loop's tool-result re-injection (Phase 1) is generic over any tool, so verifying the tool + hook at the ToolManager level is sufficient evidence for "memory_search tool_result flows back."

- [ ] **Step 1: Write the integration test**

```python
# tests/test_memory_integration.py
import asyncio
import pytest
from twinkle.agentserver.hooks.base import HookContext, HookEvent, ModelCallInputs
from twinkle.agentserver.hooks.builtin.memory_hook import MemoryHook
from twinkle.agentserver.memory import _set_memory_manager
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.tools import tool_manager


@pytest.fixture
def memory_enabled(tmp_path):
    _set_memory_manager(MemoryManager(str(tmp_path), embed_provider=None))
    yield tmp_path
    _set_memory_manager(None)


def test_cross_session_recall_via_toolmanager(memory_enabled):
    """Session A writes a fact; Session B searches and the tool returns the hit.
    Mirrors the spec acceptance: A.write -> B.search hits."""
    tm = tool_manager()
    # Session A: write
    out = asyncio.run(tm.execute("write_memory",
                                 {"path": "MEMORY.md",
                                  "content": "用户偏好用中文交流。",
                                  "append": True}))
    assert "Stored" in out

    # Session B (separate process would share the same MEMORY_DIR on disk): search
    hits = asyncio.run(tm.execute("memory_search", {"query": "用户语言偏好"}))
    assert "偏好" in hits  # the fact is recalled as a tool_result string


def test_hook_injects_then_tool_answers(memory_enabled):
    """MemoryHook injects the usage-strategy prompt on a populated store; the
    memory_search tool then returns a hit — proving the hook + tool cooperate."""
    tm = tool_manager()
    asyncio.run(tm.execute("write_memory",
                           {"path": "MEMORY.md",
                            "content": "项目架构是两进程 WebSocket。",
                            "append": True}))
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                      inputs=ModelCallInputs(messages=[{"role": "user", "content": "上次说的架构是啥"}], tools=[]),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_model_call(ctx))
    # hook injected the prompt
    assert ctx.inputs.messages[0]["role"] == "system"
    assert "memory_search" in ctx.inputs.messages[0]["content"]
    # and the tool actually returns a hit for the populated store
    hits = asyncio.run(tm.execute("memory_search", {"query": "架构"}))
    assert "WebSocket" in hits


def test_empty_store_hook_noop(memory_enabled):
    ctx = HookContext(agent=None, event=HookEvent.BEFORE_MODEL_CALL,
                      inputs=ModelCallInputs(messages=[{"role": "user", "content": "hi"}], tools=[]),
                      session_id="s", request_id="r")
    asyncio.run(MemoryHook().before_model_call(ctx))
    assert ctx.inputs.messages == [{"role": "user", "content": "hi"}]
```

> Verify `ToolManager.execute` returns the tool's string result directly. If it returns a wrapper, adjust the assertions to read `.result` or equivalent — check `twinkle/agentserver/tools/manager.py::ToolManager.execute` signature. (It returns `str` for `LocalFunction` tools, matching the existing skill/todo tools.)

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_integration.py -v`
Expected: PASS (3 tests).

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory_integration.py
git commit -m "memory: integration test — cross-session recall via ToolManager + hook injection"
```

---

## Self-Review

**Spec coverage** (spec §1 5a scope → tasks):
- markdown + SQLite(6表) + sqlite-vec + FTS5 + embedding cache → Task 2 (schema) + Task 4 (vec)
- 4 `@tool` → Task 7
- `MemoryHook`(before_model_call, proactive) → Task 8
- embedding provider (OpenAI-compat + Mock test-only) → Task 1
- pure model-driven (no auto-inject) → Task 11 removes the only `recall()` call site; nothing injects results
- spec §2 storage layout / per-file routing / prompt draft / 6-table schema / retrieval params → Tasks 2, 8 (prompt), 9 (config)
- spec §3 flows (write/store, cleanup, query) → Tasks 3, 5, 3 respectively
- spec §4 config → Task 9
- spec §6 error handling (DB-fail no-op, embed-fail FTS degrade, path越界 string, model-change rebuild) → Tasks 2 (path), 3 (embed-fail), 5 (rebuild); DB-init fail → MemoryManager `__init__` opens DB inside `sqlite3.connect` (raises on bad path — wrap if needed; for 5a the dir is created by ensure_workspace_dir, so this is low-risk; a follow-up could wrap in try/except but YAGNI for MVP)
- spec §7 tests → Tasks 1,2,3,4,5,7,8,9,12
- spec §8 file list → all tasks; permissions.tools → Task 9; `[memory]` extra → Task 11; roadmap revision → noted in spec §8 (not a code task here; update roadmap.md in the PR)
- spec §10 acceptance → Task 12 covers cross-session recall + hook; FTS-only degrade → Task 4; path越界 → Task 3; edit re-index → Task 5

**Gaps noted, not blockers:**
- DB-init-failure no-op wrapping isn't a dedicated task — `MemoryManager.__init__` will raise if `memory.db` is unwritable. The store lives under `<WORKSPACE>/.twinkle_data/memory/` created by `ensure_workspace_dir`, so the failure mode is narrow. Flag for a hardening follow-up if encountered.
- `read_memory` offset/limit = line-based (resolved gap #4) — Task 2 test pins it.
- meta model-check timing (gap #5) — runs in `__init__` + callable at runtime (Task 5 test calls `_check_model_changed` directly).
- sync embed in async @tool blocks event loop (gap #7) — documented in spec; acceptable for 5a.

**Placeholder scan:** none — every step has complete code or an exact command.
**Type consistency:** `MemoryManager.search` returns `list[dict]` with `path`/`score`/`text` keys (Tasks 3,4) — consumed by `memory_search` tool (Task 7) and integration test (Task 12). `get_memory_manager()` / `_set_memory_manager()` defined Task 6, used Tasks 7,8,12. `MemoryHook.priority=80` (Task 8) < `SkillHook.priority=90` — verified Task 11 wiring test. Tool names `memory_search`/`write_memory`/`read_memory`/`edit_memory` consistent across Task 7 (defs), Task 9 (permissions keys), Task 11 (wiring).

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-07-27-long-term-memory.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
