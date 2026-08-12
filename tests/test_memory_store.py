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


def test_resolve_relative_path_whitelist(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr._resolve_relative_path("USER.md") == "USER.md"               # noqa: SLF001
    assert mgr._resolve_relative_path("MEMORY.md") == "MEMORY.md"
    assert mgr._resolve_relative_path("daily_memory/2026-07-27.md") == "daily_memory/2026-07-27.md"
    assert mgr._resolve_relative_path("../escape.md") is None
    assert mgr._resolve_relative_path("sub/dir/MEMORY.md") is None
    assert mgr._resolve_relative_path("daily_memory/notadate.md") is None
    assert mgr._resolve_relative_path("daily_memory/2026-07-27.txt") is None


def test_list_files_empty(tmp_path):
    assert _mgr(tmp_path).list_files() == []


def test_list_files_filters_non_whitelist(tmp_path):
    """list_files only returns whitelist paths (USER.md/MEMORY.md/daily_memory/
    YYYY-MM-DD.md) — a stray .md elsewhere in the dir must not surface, else
    MemoryHook would inject on a non-memory file."""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "a fact", append=True)
    (tmp_path / "notes.md").write_text("stray", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "extra.md").write_text("nested stray", encoding="utf-8")
    assert mgr.list_files() == ["MEMORY.md"]


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


def test_write_round_trips_via_nonclean_path():
    """Regression: _resolve_relative_path compared a resolve()'d path against an
    un-resolved self._dir, breaking write/read on Windows short-name paths
    (e.g. C:/Users/WANGGU~1/... from tempfile.mkdtemp). __init__ now stores
    self._dir resolved so is_relative_to stays consistent."""
    import tempfile
    d = tempfile.mkdtemp()
    mgr = MemoryManager(str(d), embed_provider=None)
    out = mgr.write("MEMORY.md", "fact via non-clean path", append=True)
    assert "Stored" in out, f"write failed on non-clean path: {out!r}"
    assert "fact via non-clean path" in mgr.read("MEMORY.md")
    assert mgr._dir == mgr._dir.resolve()


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


def test_model_change_clears_index(tmp_path):
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8, model="v1"),
                        dims=8)
    mgr.write("MEMORY.md", "some fact", append=True)
    assert mgr._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    # swap provider to a different model name -> clear stale index
    mgr._provider = MockEmbeddingProvider(dims=8, model="v2")
    mgr._clear_if_model_changed()
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


def test_index_file_rolls_back_on_insert_error(tmp_path):
    """A failed DB statement mid-index must roll back so the next write on the
    shared singleton connection doesn't commit the broken file's partial state.
    Trigger: vec0 table dims=4 but provider returns 8-float vectors -> INSERT
    mismatch raises inside the chunk loop. Without rollback the chunks INSERT
    stays open (uncommitted, read-your-writes) and files/meta never get stamped."""
    pytest.importorskip("sqlite_vec")
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8),
                        dims=4)  # vec0 table float[4] vs 8-float vectors
    with pytest.raises(Exception):
        mgr.write("MEMORY.md", "some fact", append=True)
    # rollback: nothing committed for this file
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM files WHERE path='MEMORY.md'").fetchone()[0] == 0


def test_hybrid_search_result_has_line_numbers(tmp_path):
    """Hybrid return must carry start_line/end_line like the FTS-only return —
    the public search() contract is the same shape in both modes."""
    pytest.importorskip("sqlite_vec")
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8),
                        dims=8)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    hits = mgr.search("偏好")
    assert hits
    assert "start_line" in hits[0]
    assert "end_line" in hits[0]
