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
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("twinkle.memory")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_ROOT_FILES = ("USER.md", "MEMORY.md")
# CJK ideographs — space each one so FTS5 unicode61 (which does not split CJK
# into words) can match CJK substrings. jiuwenswarm relies on the vector leg
# for CJK recall; Twinkle's FTS-only degradation path (no API key) needs CJK
# recall too, so the FTS leg spaces each CJK char before indexing + querying.
_CJK_PAT = re.compile("[" + chr(0x3400) + "-" + chr(0x9FFF) + chr(0xF900) + "-" + chr(0xFAFF) + "]")


def _space_cjk(text: str) -> str:
    """Space each CJK char so unicode61 tokenizes into single-char tokens.
    Latin/whitespace/punctuation untouched (already split)."""
    return _CJK_PAT.sub(lambda m: " " + m.group(0) + " ", text)


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
        # check_same_thread default (True) holds: all MemoryManager access is
        # on the AgentServer event-loop thread (single-process, single-loop).
        # If a future change moves embeds/vector work to asyncio.to_thread or
        # a background thread, this connection must move with it or go per-thread.
        self._db = sqlite3.connect(str(self._dir / "memory.db"))
        self._db.row_factory = sqlite3.Row
        self._vec_enabled = False
        self._ensure_schema()
        self._rebuild_if_model_changed()

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
    def _resolve_rel_path(self, path: str) -> str | None:
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
            if not p.is_file():
                continue
            rel = p.relative_to(self._dir).as_posix()
            if self._resolve_rel_path(rel) is not None:
                out.append(rel)
        return out

    def read(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        rel = self._resolve_rel_path(path)
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

    # --- writing + indexing ----------------------------------------------
    def write(self, path: str, content: str, append: bool = False) -> str:
        rel = self._resolve_rel_path(path)
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
        rel = self._resolve_rel_path(path)
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

        # Wrap the whole mutation in a transaction so a mid-index failure
        # (e.g. a vec0 dims-mismatch INSERT) rolls back cleanly. Without this
        # the open transaction leaks onto the next write on this singleton
        # connection and commits the broken file's partial state. Mirrors
        # jiuwenswarm manager.py _index_file try/rollback.
        try:
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
            texts = [c.text for c in chunks]
            embeddings = self._embed_chunks(texts) if want_vec else [None] * len(chunks)
            now = _now_iso()
            model = self._provider.model if self._provider else ""
            for chunk, emb in zip(chunks, embeddings):
                cid = f"{rel}:{chunk.start}:{chunk.end}"
                # _embed_chunks already returns serialized blobs (it calls
                # _serialize on the float list). Don't double-serialize — that
                # treats the blob's bytes as a float list and inflates dims
                # (8 floats -> 32-byte blob -> 32 dims -> vec0 mismatch).
                blob = emb
                cur = self._db.execute(
                    "INSERT INTO chunks(id,path,source,start_line,end_line,hash,model,"
                    "text,embedding,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, rel, "memory", chunk.start, chunk.end, file_hash, model,
                     chunk.text, blob, now))
                rowid = cur.lastrowid
                self._db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                                 (rowid, _space_cjk(chunk.text)))
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

    def _enforce_cap(self, rel: str) -> None:
        count = self._db.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        if count <= self._max_chunks_per_file:
            return
        excess = count - self._max_chunks_per_file
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

    def _rebuild_if_model_changed(self) -> None:
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
        max_results = max_results or self._max_results
        candidates = min(200, max(1, int(max_results * self._candidate_multiplier)))
        fts = self._fts_search(query, candidates)  # ORDER BY bm -> best first
        if not (self._vec_enabled and self._provider is not None):
            fts_only = fts[:max_results]
            log.info("memory_search query=%r hits=%d (fts-only)", query, len(fts_only))
            return [self._hit(r, self._text_sim(r["bm"])) for r in fts_only]
        vec: dict[int, float] = self._vec_search(query, candidates)
        fts_bm = {r["rowid"]: r for r in fts}  # rowid -> fts row (carries bm)
        chunk_map = dict(fts_bm)
        for rid in vec:
            if rid not in chunk_map:
                cr = self._db.execute(
                    "SELECT rowid, path, text, start_line, end_line "
                    "FROM chunks WHERE rowid=?", (rid,)).fetchone()
                if cr:
                    chunk_map[rid] = cr
        fused: list[tuple] = []
        for rid, chunk in chunk_map.items():
            if chunk is None:
                continue
            t = self._text_sim(fts_bm[rid]["bm"]) if rid in fts_bm else 0.0
            v = vec.get(rid, 0.0)
            fused.append((chunk, self._vector_weight * v + self._text_weight * t))
        fused.sort(key=lambda x: x[1], reverse=True)
        fused = fused[:max_results]
        log.info("memory_search query=%r hits=%d (hybrid)", query, len(fused))
        return [self._hit(r, s) for r, s in fused]

    def _fts_search(self, query: str, limit: int):
        phrase = '"' + _space_cjk(query).replace('"', '""') + '"'
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
