"""MemoryManager — markdown-authoritative + SQLite-retrieval long-term memory.

Mirrors jiuwenswarm's MemoryIndexManager (6 tables, hybrid vector+FTS, mtime
incremental indexing, embedding cache) but slimmed for Twinkle. Vectors are
opt-in (sqlite-vec optional extra); without it or without an embedding API key,
search degrades to FTS-only. All public methods return strings or lists — never
raise on bad input (errors go through returned strings, like skill_tools).
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

from twinkle.agentserver.memory.fts import build_fts_query, tokenize_for_fts

log = logging.getLogger("twinkle.memory")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_ROOT_FILES = ("USER.md", "MEMORY.md")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


class Chunk(NamedTuple):
    start: int
    end: int
    text: str


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
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
        candidate_multiplier: float = 2.0,
        max_chunks_per_file: int = 200,
        index_debounce_seconds: float = 2.0,
    ) -> None:
        self._dir = Path(memory_dir).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "daily_memory").mkdir(exist_ok=True)
        self._provider = embed_provider
        self._dims = dims
        self._chunk_tokens = chunk_tokens
        self._chunk_overlap = chunk_overlap
        self._max_results = max_results
        self._vector_weight = vector_weight
        self._text_weight = text_weight
        self._candidate_multiplier = candidate_multiplier
        self._max_chunks_per_file = max_chunks_per_file
        self._debounce = index_debounce_seconds
        # 防抖:写入路径零索引(write/edit/replace 只落盘标 dirty,不调 _index_file);
        # 索引由 search 兜底(if dirty 同步)或后台 threading.Timer 异步做——对齐
        # jiuwenswarm 写入零索引 + watchDebounceMs 去抖(省 watchdog:write 在
        # manager 内直接 mark_dirty,不需文件监听桥)。check_same_thread=False +
        # RLock:_flush_dirty 跑在 timer 线程,与主线程 search/flush_now 并发访问 SQLite。
        self._db = sqlite3.connect(str(self._dir / "memory.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db_lock = threading.RLock()  # 重入:_index_file 持锁调 _embed_chunks/_evict
        self._dirty_paths: set[str] = set()
        self._dirty_lock = threading.Lock()  # 保护 _dirty_paths + _sync_timer(主线程 add / timer 线程 drain)
        self._sync_timer: threading.Timer | None = None
        self._vec_enabled = False
        self._ensure_schema()
        self._clear_if_model_changed()

    @property
    def memory_dir(self) -> Path:
        """Resolved memory directory (read-only). Exposed for dreaming's sidecar
        (dreaming_state.json) which lives next to MEMORY.md but outside the
        write-whitelist (raw pathlib, not mgr.write)."""
        return self._dir

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
    def _resolve_relative_path(self, path: str) -> str | None:
        """Whitelist: USER.md / MEMORY.md (root) or daily_memory/YYYY-MM-DD.md.
        Returns the resolved-relative path, or None if invalid."""
        if path in _ROOT_FILES:
            relative_path = path
        elif path.startswith("daily_memory/"):
            tail = path[len("daily_memory/"):]
            if not _DATE_RE.match(tail):
                return None
            relative_path = path
        else:
            return None
        try:
            resolved = (self._dir / relative_path).resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self._dir):
            return None
        return relative_path

    # --- listing / reading -----------------------------------------------
    def list_files(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self._dir.rglob("*.md")):
            if not p.is_file():
                continue
            relative_path = p.relative_to(self._dir).as_posix()
            if self._resolve_relative_path(relative_path) is not None:
                out.append(relative_path)
        return out

    def read(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        relative_path = self._resolve_relative_path(path)
        if relative_path is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / relative_path
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

    # --- writing + indexing ----------------------------------------------
    def write(self, path: str, content: str, append: bool = False) -> str:
        relative_path = self._resolve_relative_path(path)
        if relative_path is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / relative_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with fpath.open("a" if append else "w", encoding="utf-8") as f:
                f.write(content)
                if append and not content.endswith("\n"):
                    f.write("\n")
        except OSError as exc:
            return f"Error writing '{path}': {exc}"
        self._mark_dirty(relative_path)
        log.info("write_memory path=%s append=%s", relative_path, append)
        return f"Stored to {relative_path}."

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        relative_path = self._resolve_relative_path(path)
        if relative_path is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / relative_path
        if not fpath.is_file():
            return f"Error: '{path}' not found."
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"Error reading '{path}': {exc}"
        if old_text not in text:
            return f"Error: old_text not found in '{path}'."
        fpath.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        self._mark_dirty(relative_path)
        log.info("edit_memory path=%s", relative_path)
        return f"Edited {relative_path}."

    def replace(self, path: str, content: str) -> str:
        """Atomic full overwrite — write to a temp file then rename onto the
        target in one step (rename is atomic on the same filesystem). Used by
        dreaming's consolidation step to rewrite MEMORY.md after a read
        snapshot: the atomic rename prevents a torn write racing an agent
        append, and the rebuild via _index_file reflects the new content."""
        relative_path = self._resolve_relative_path(path)
        if relative_path is None:
            return (f"Error: invalid memory path '{path}'. "
                    "Allowed: USER.md, MEMORY.md, daily_memory/YYYY-MM-DD.md.")
        fpath = self._dir / relative_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = fpath.parent / (fpath.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(fpath)
        except OSError as exc:
            tmp.unlink(missing_ok=True)  # 别留 .tmp 残留
            return f"Error replacing '{path}': {exc}"
        self._mark_dirty(relative_path)
        log.info("replace_memory path=%s", relative_path)
        return f"Replaced {relative_path}."

    # --- debounce: 写入零索引, 索引由 search 兜底 / 后台 timer 异步 ----------
    def _mark_dirty(self, relative_path: str) -> None:
        """标记文件 dirty(待索引)+ 排程去抖 sync。写入路径不碰 DB 索引(零
        embedding API,强化 B §7 写入快通道)。_dirty_paths 跨线程:主线程 add,
        timer 线程 drain。"""
        with self._dirty_lock:
            self._dirty_paths.add(relative_path)
            if self._sync_timer:
                self._sync_timer.cancel()
            self._sync_timer = threading.Timer(self._debounce, self._flush_dirty)
            self._sync_timer.start()

    def _drain_dirty(self) -> list[str]:
        """取出并清空 dirty 集 + 取消 pending timer(主线程 _flush_now 和 timer
        线程 _flush_dirty 都走这里,pop 原子在锁内防重复索引)。"""
        with self._dirty_lock:
            if self._sync_timer:
                self._sync_timer.cancel()
                self._sync_timer = None
            paths = sorted(self._dirty_paths)
            self._dirty_paths.clear()
            return paths

    def _flush_dirty(self) -> None:
        """timer 线程:去抖窗口到期后批量重索引 dirty 文件(文件级 hash 跳过未变)。
        与主线程 search 并发 → _index_file 持 _db_lock(RLock)互斥。"""
        for path in self._drain_dirty():
            self._index_file(path)

    def _flush_now(self) -> None:
        """主线程同步重索引 dirty(search 兜底 / 测试用)。取消 pending timer
        避免重复,立即索引保证 search 搜到刚写的内容。"""
        for path in self._drain_dirty():
            self._index_file(path)

    def _index_file(self, relative_path: str) -> None:
        fpath = self._dir / relative_path
        try:
            stat = fpath.stat()
            content = fpath.read_text(encoding="utf-8")
        except OSError:
            return
        file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        # 防抖后 _index_file 跑在 timer 线程(_flush_dirty)或主线程(search 兜底
        # _flush_now),跨线程并发 → _db_lock(RLock)互斥。文件 stat/read/hash 在锁外。
        with self._db_lock:
            fingerprint = self._db.execute(
                "SELECT mtime, size, hash FROM files WHERE path=?", (relative_path,)).fetchone()
            if (fingerprint and fingerprint["mtime"] == stat.st_mtime
                    and fingerprint["size"] == stat.st_size and fingerprint["hash"] == file_hash):
                return  # unchanged — skip re-index

            # Wrap the whole mutation in a transaction so a mid-index failure
            # (e.g. a vec0 dims-mismatch INSERT) rolls back cleanly. Without this
            # the open transaction leaks onto the next write on this singleton
            # connection and commits the broken file's partial state. Mirrors
            # jiuwenswarm manager.py _index_file try/rollback.
            try:
                # delete old chunks for this file (collect rowids first)
                stale_row_ids = [r["rowid"] for r in self._db.execute(
                    "SELECT rowid FROM chunks WHERE path=?", (relative_path,)).fetchall()]
                if stale_row_ids:
                    placeholders = ",".join("?" * len(stale_row_ids))
                    self._db.execute("DELETE FROM chunks WHERE path=?", (relative_path,))
                    self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", stale_row_ids)
                    if self._vec_enabled:
                        self._db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", stale_row_ids)

                want_vec = self._vec_enabled and self._provider is not None
                chunks = self._chunk(content)
                texts = [c.text for c in chunks]
                embeddings = self._embed_chunks(texts) if want_vec else [None] * len(chunks)
                now = _now_iso()
                embed_model = self._provider.model if self._provider else ""
                for chunk, emb in zip(chunks, embeddings):
                    chunk_id = f"{relative_path}:{chunk.start}:{chunk.end}"
                    # _embed_chunks already returns serialized blobs (it calls
                    # _serialize on the float list). Don't double-serialize — that
                    # treats the blob's bytes as a float list and inflates dims
                    # (8 floats -> 32-byte blob -> 32 dims -> vec0 mismatch).
                    blob = emb
                    cur = self._db.execute(
                        "INSERT INTO chunks(id,path,source,start_line,end_line,hash,model,"
                        "text,embedding,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (chunk_id, relative_path, "memory", chunk.start, chunk.end, file_hash, embed_model,
                         chunk.text, blob, now))
                    rowid = cur.lastrowid
                    self._db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                                     (rowid, tokenize_for_fts(chunk.text, True)))
                    if want_vec and blob is not None:
                        self._db.execute(
                            "INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)",
                            (rowid, blob))
                self._db.execute(
                    "INSERT OR REPLACE INTO files(path,source,hash,mtime,size) VALUES(?,?,?,?,?)",
                    (relative_path, "memory", file_hash, stat.st_mtime, stat.st_size))
                self._db.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('embed_model', ?)",
                    (embed_model,))
                self._evict_excess_chunks(relative_path)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

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
                size += len(lines[j]) + 1
                j += 1
            out.append(Chunk(i + 1, j, "\n".join(lines[i:j])))
            if j >= n:
                break
            # backtrack into the tail for overlap so the next chunk shares lines
            overlap_bytes, backtrack_to = 0, j
            while backtrack_to > i + 1 and overlap_bytes < overlap:
                backtrack_to -= 1
                overlap_bytes += len(lines[backtrack_to]) + 1
            i = backtrack_to if backtrack_to > i else j
        return out

    def _embed_chunks(self, texts: list[str]) -> list:
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
                now = _now_iso()
                for (h, _), vec in zip(to_embed, vecs):
                    blob = self._serialize(vec)
                    self._db.execute(
                        "INSERT OR REPLACE INTO embedding_cache"
                        "(hash,embedding,dims,updated_at) VALUES(?,?,?,?)",
                        (h, blob, self._dims, now))
                    new[h] = blob
            except Exception as exc:
                # Chunks are FTS-indexed below without vectors; the files/meta
                # stamp in _index_file means they won't auto-retry until the
                # file content changes again (no retry scheduler in 5a).
                log.warning("embedding failed; chunks indexed FTS-only (no vectors), "
                            "not retried until file changes: %s", exc)
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

    def _evict_excess_chunks(self, relative_path: str) -> None:
        chunk_count = self._db.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=?", (relative_path,)).fetchone()[0]
        if chunk_count <= self._max_chunks_per_file:
            return
        num_to_evict = chunk_count - self._max_chunks_per_file
        oldest_rowids = [r["rowid"] for r in self._db.execute(
            "SELECT rowid FROM chunks WHERE path=? ORDER BY updated_at ASC LIMIT ?",
            (relative_path, num_to_evict)).fetchall()]
        if not oldest_rowids:
            return
        placeholders = ",".join("?" * len(oldest_rowids))
        self._db.execute(f"DELETE FROM chunks WHERE rowid IN ({placeholders})", oldest_rowids)
        self._db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", oldest_rowids)
        if self._vec_enabled:
            self._db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", oldest_rowids)

    def _clear_if_model_changed(self) -> None:
        if self._provider is None:
            return
        row = self._db.execute(
            "SELECT value FROM meta WHERE key='embed_model'").fetchone()
        if row and row["value"] and row["value"] != self._provider.model:
            log.warning("embed model %r -> %r, clearing stale index",
                        row["value"], self._provider.model)
            self._db.execute("DELETE FROM chunks")
            self._db.execute("DELETE FROM chunks_fts")
            if self._vec_enabled:
                self._db.execute("DELETE FROM chunks_vec")
            self._db.execute("DELETE FROM embedding_cache")
            self._db.execute("DELETE FROM files")
            # clear the stale embed_model stamp — the next _index_file call
            # re-stamps it via INSERT OR REPLACE. Leaving it would make the
            # recorded model name outlive the chunks it described.
            self._db.execute("DELETE FROM meta WHERE key='embed_model'")
            self._db.commit()

    # --- search ----------------------------------------------------------
    def search(self, query: str, max_results: int | None = None) -> list[dict]:
        """Hybrid retrieval, ranked + top-N capped (no score cutoff, mirroring
        jiuwenswarm's no-cutoff FTS). FTS-only (no sqlite-vec or no provider):
        matches in SQL bm25 order (best first). Hybrid: fuse vector similarity
        + FTS bm25 score (both best=high) and rank by the fused score."""
        # 兜底:写入零索引后,刚写的内容可能还在 dirty 集未索引→搜不到。
        # search 前同步 flush 保证可见性(对齐 jiuwenswarm search 前确保索引最新)。
        if self._dirty_paths:
            self._flush_now()
        max_results = max_results or self._max_results
        candidates = min(200, max(1, int(max_results * self._candidate_multiplier)))
        with self._db_lock:
            fts_rows = self._fts_search(query, candidates)  # ORDER BY bm -> best first
            fts_by_rowid = {r["rowid"]: r for r in fts_rows}

            if not (self._vec_enabled and self._provider is not None):
                hits = fts_rows[:max_results]
                log.info("memory_search query=%r hits=%d (fts-only)", query, len(hits))
                return [self._hit(r, self._text_sim(r["bm"])) for r in hits]

            vec_sims = self._vec_search(query, candidates)  # {rowid: 相似度}

            # 候选 = 两路并集;fts 行已带正文,vec-only 的去主表回填
            candidate_rows = dict(fts_by_rowid)
            for row_id in vec_sims:
                if row_id not in candidate_rows:
                    cr = self._db.execute(
                        "SELECT rowid, path, text, start_line, end_line "
                        "FROM chunks WHERE rowid=?", (row_id,)).fetchone()
                    if cr:
                        candidate_rows[row_id] = cr

            # 融合打分:向量相似度 + 文本相似度,加权
            scored = []
            for row_id, row in candidate_rows.items():
                text_sim = self._text_sim(fts_by_rowid[row_id]["bm"]) if row_id in fts_by_rowid else 0.0
                vec_sim = vec_sims.get(row_id, 0.0)
                fused = self._vector_weight * vec_sim + self._text_weight * text_sim
                scored.append((row, fused))

            # 排序、截断、格式化
            scored.sort(key=lambda x: x[1], reverse=True)
            scored = scored[:max_results]
            log.info("memory_search query=%r hits=%d (hybrid)", query, len(scored))
            return [self._hit(row, score) for row, score in scored]

    def _fts_search(self, query: str, limit: int):
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        return self._db.execute(
            "SELECT c.rowid, c.path, c.text, c.start_line, c.end_line, "
            "bm25(chunks_fts) AS bm FROM chunks_fts JOIN chunks c "
            "ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? "
            "ORDER BY bm LIMIT ?",
            (fts_query, limit)).fetchall()

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
    def _hit(row, score: float) -> dict:
        """Uniform result shape for both FTS-only + hybrid paths (path/score/
        text/start_line/end_line). Centralizing prevents shape drift."""
        return {"path": row["path"], "score": round(score, 4),
                "text": row["text"], "start_line": row["start_line"],
                "end_line": row["end_line"]}

    @staticmethod
    def _text_sim(bm: float) -> float:
        # bm25() is <= 0 for matches (more negative = more relevant). Map to
        # [0,1] with best (most negative) -> ~1.0. No score cutoff is applied
        # (see search()) — retrieval is ranked + top-N capped.
        a = abs(bm)
        return a / (1.0 + a)
