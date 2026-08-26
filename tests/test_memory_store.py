import sqlite3
import pytest
from twinkle.agentserver.memory.store import MemoryManager


def _mgr(tmp_path, **kw):
    return MemoryManager(str(tmp_path), embed_provider=None, **kw)


def test_schema_creates_six_tables(tmp_path):
    mgr = _mgr(tmp_path)
    db = mgr._db  # noqa: SLF001 — 测试探查内部句柄
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
    """list_files 只返回白名单路径（USER.md/MEMORY.md/daily_memory/
    YYYY-MM-DD.md）——目录里散落的 .md 不应出现，否则 MemoryHook 会
    在非 memory 文件上注入。"""
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


def test_write_does_not_index_until_flush(tmp_path):
    """防抖:write 只落盘标 dirty,不立即索引(DB 无 chunks);search 开头 if dirty
    同步兜底后搜到;_flush_now 后 DB 有索引。

    对齐 jiuwenswarm 写入零索引 + 去抖:写入路径不碰检索索引(强化 B §7 写入快通道
    零 API),索引由 search 兜底或后台 timer 异步做。write 不再触发 _index_file
    全量重索引——这是降频核心。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    # 防抖未 flush → DB 无索引(写入路径零索引)
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    # search 兜底:开头 if dirty 同步索引后搜到
    hits = mgr.search("偏好")
    assert any("偏好" in h["text"] for h in hits)
    # flush 后 DB 有索引
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] >= 1


def test_search_fts_only_hits_written_fact(tmp_path):
    """无 embed_provider → FTS-only；写一条事实，按关键词搜索，命中。"""
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


def test_search_fts_phrase_bug_regression(tmp_path):
    """Regression: 多 token 自然语言 query 必须召回措辞不同的记忆。旧 _fts_search
    把整句包双引号喂 FTS5 = phrase(所有 token 须按序连续)→ 任何换措辞的 query 0
    命中,FTS 腿实际废掉。build_fts_query 现按 token 切分 + OR 连接,任一共享
    token(如 决定 / SQLite)即命中。"""
    mgr = _mgr(tmp_path)  # embed_provider=None → FTS-only(最易暴露 phrase bug)
    mgr.write("MEMORY.md", "决定使用 SQLite,考虑部署简单。", append=True)
    hits = mgr.search("我们之前为什么决定用 SQLite")
    assert hits, "换措辞的多 token query 必须召回(phrase bug 回归)"
    assert any("SQLite" in h["text"] for h in hits)


def test_search_fts_jieba_word_level(tmp_path):
    """jieba 词级分词路径:换措辞多 token query 仍召回。jieba 把 query 切成词,
    滤停用词,OR 连接——验证 jieba 路径(装了 jieba 时)不崩 + 召回合理。"""
    pytest.importorskip("jieba")
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好简洁的中文回复,不喜欢长篇大论。", append=True)
    hits = mgr.search("用户喜欢简短回答")
    assert hits, "jieba 词级分词应让换措辞 query 召回"
    assert any("用户" in h["text"] or "偏好" in h["text"] for h in hits)


def test_search_fts_degraded_no_jieba(tmp_path, monkeypatch):
    """无 jieba → 降级 _space_cjk 逐字 OR 路径仍召回(比 phrase 好)。monkeypatch
    拦 jieba import 强制走降级分支,验证无论 jieba 装否降级都不破。"""
    import builtins
    real_import = builtins.__import__

    def _block_jieba(name, *args, **kwargs):
        if name == "jieba":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_jieba)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "决定使用 SQLite,考虑部署简单。", append=True)
    hits = mgr.search("为什么决定用 SQLite")
    assert hits, "降级逐字 OR 路径应召回"
    assert any("SQLite" in h["text"] for h in hits)


def test_search_logs(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="twinkle.memory")
    _mgr(tmp_path).search("x")
    assert any("memory_search" in r.message for r in caplog.records)


def test_write_round_trips_via_nonclean_path():
    """Regression：_resolve_relative_path 拿 resolve() 后的路径去比未 resolve 的
    self._dir，导致 Windows 短名路径（如 tempfile.mkdtemp 产生的
    C:/Users/WANGGU~1/...）上 write/read 失效。__init__ 现在存的是 resolved 后的
    self._dir，使 is_relative_to 保持一致。"""
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
    # FTS 腿保证正确 chunk 排前；hybrid 融合不破坏它
    assert any("偏好" in h["text"] for h in hits)


def test_mtv_degrades_to_fts_only_when_no_provider(tmp_path):
    """装了 sqlite-vec 但无 provider（无 API key）→ FTS-only，无 vector 腿。"""
    pytest.importorskip("sqlite_vec")
    mgr = MemoryManager(str(tmp_path), embed_provider=None)  # no provider
    assert mgr._vec_enabled  # noqa: SLF001 — extension loaded
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    hits = mgr.search("偏好")
    assert any("偏好" in h["text"] for h in hits)  # FTS 仍可用


def test_edit_replaces_and_reindexes(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "用户偏好英文。", append=True)
    mgr.edit("MEMORY.md", "英文", "中文")
    assert "用户偏好中文。" in mgr.read("MEMORY.md")
    assert "英文" not in mgr.read("MEMORY.md")
    # 旧文本不再可召回，新文本可召回
    assert any("中文" in h["text"] for h in mgr.search("偏好"))
    assert not any("英文" in h["text"] for h in mgr.search("偏好"))


def test_edit_old_text_missing(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "hello", append=True)
    out = mgr.edit("MEMORY.md", "nope", "x")
    assert "not found" in out.lower()


def test_replace_full_overwrite_and_reindexes(tmp_path):
    """replace 原子全量覆写:旧内容全清(非追加)、索引按新内容重建(旧 chunk
    不再可召回)、不留 .tmp 残留。dreaming 整合步用 replace 重写 MEMORY.md——
    read 快照后整文件换写,必须原子(tempfile+rename)且重建索引。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 用 Windows 系统\n", append=True)
    mgr.write("MEMORY.md", "- 偏好中文\n", append=True)
    out = mgr.replace("MEMORY.md", "- 用 macOS 系统\n")
    assert "Replaced" in out
    assert mgr.read("MEMORY.md") == "- 用 macOS 系统"  # 旧内容全清,非追加
    # 索引按新内容重建:旧 chunk 不再召回,新 chunk 可召回
    assert not any("Windows" in h["text"] for h in mgr.search("Windows"))
    assert any("macOS" in h["text"] for h in mgr.search("macOS"))
    # 原子写不留 .tmp 残留
    assert not list(tmp_path.rglob("*.tmp"))


def test_model_change_clears_index(tmp_path):
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8, model="v1"),
                        dims=8)
    mgr.write("MEMORY.md", "some fact", append=True)
    mgr._flush_now()  # noqa: SLF001 — 防抖:write 零索引,显式 flush 落索引后再断言
    assert mgr._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    # 换成不同模型名的 provider → 清掉过期索引
    mgr._provider = MockEmbeddingProvider(dims=8, model="v2")
    mgr._clear_if_model_changed()
    assert mgr._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert mgr._db.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone() is None


def test_fifo_cap_evicts_oldest(tmp_path):
    mgr = MemoryManager(str(tmp_path), embed_provider=None, max_chunks_per_file=2)
    # 每次写入都重建索引（覆写该 file 的 chunks）；要超 cap 需在单文件里
    # 有 >2 个 chunks。写一段含 3+ chunks 的长内容。
    long_content = "\n".join(f"line {i} has unique content number {i}" for i in range(20))
    mgr.write("MEMORY.md", long_content, append=False)
    mgr._flush_now()  # noqa: SLF001 — 防抖:write 零索引,显式 flush 落索引后再断言 cap
    count = mgr._db.execute("SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0]
    assert count <= 2  # 触顶 cap


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
    """索引中途某条 DB 语句失败必须 rollback，否则下一次写共享单例连接会
    提交该文件的半截状态。触发方式：vec0 表 dims=4 但 provider 返回
    8 维向量 → chunk 循环内 INSERT 维度不匹配抛异常。若不 rollback，chunks
    的 INSERT 会留着未提交（read-your-writes），files/meta 也永远不会标戳。"""
    pytest.importorskip("sqlite_vec")
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8),
                        dims=4)  # vec0 表 float[4] 对 8 维向量
    mgr.write("MEMORY.md", "some fact", append=True)
    # 防抖:write 零索引不触发 _index_file → 不引发;显式调 _index_file 触发
    # vec0 维度不匹配 INSERT,验证 rollback(files/meta 无残留)。
    with pytest.raises(Exception):
        mgr._index_file("MEMORY.md")  # noqa: SLF001
    # rollback：该文件无任何提交残留
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM chunks WHERE path='MEMORY.md'").fetchone()[0] == 0
    assert mgr._db.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM files WHERE path='MEMORY.md'").fetchone()[0] == 0


def test_hybrid_search_result_has_line_numbers(tmp_path):
    """Hybrid 返回必须像 FTS-only 返回一样带 start_line/end_line——
    两种模式下公开 search() 契约的返回形状一致。"""
    pytest.importorskip("sqlite_vec")
    from twinkle.agentserver.memory.embeddings import MockEmbeddingProvider
    mgr = MemoryManager(str(tmp_path), embed_provider=MockEmbeddingProvider(dims=8),
                        dims=8)
    mgr.write("MEMORY.md", "用户偏好中文。", append=True)
    hits = mgr.search("偏好")
    assert hits
    assert "start_line" in hits[0]
    assert "end_line" in hits[0]
