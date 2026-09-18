"""memory 包 —— 重新导出 + 进程级单例(对齐 skills/__init__)。"""
import logging
from pathlib import Path

from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.memory.embeddings import (
    EmbeddingProvider,
    MockEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    SentenceTransformerProvider,
)

log = logging.getLogger("twinkle.memory")

_MEMORY_MANAGER: MemoryManager | None = None


def _build_embed_provider():
    """从 config 选 embedding provider + dims。纯函数(读 twinkle.config 模块常量,
    无单例副作用),供 get_memory_manager + 测试调。

    loud-degrade:local 缺依赖/缺模型 → log.error 一次 + 返回 (None, dims)(走诚实
    FTS-only),不 hard raise——memory 是可选子系统,agent 不该因 embed 缺失挂死。
    本次 bug 的根因即"静默吞 404 + 谎报 (hybrid)",此处反其道:大声报 + 诚实降级。
    - none: None(纯 FTS,不调 embed)。
    - openai_compat: 线上 HTTP;embed.base_url/api_key 空→复用 llm.*;无 key→warning+None。
    - local: 进程内 sentence-transformers;import 失败或 model_path 不存在→error+None。
    """
    from twinkle.config import (
        LLM_API_KEY, LLM_BASE_URL,
        MEMORY_EMBED_API_KEY, MEMORY_EMBED_BASE_URL, MEMORY_EMBED_DIMS,
        MEMORY_EMBED_MODEL, MEMORY_EMBED_MODEL_PATH, MEMORY_EMBED_PROVIDER)
    dims = MEMORY_EMBED_DIMS
    prov = MEMORY_EMBED_PROVIDER
    if prov == "none":
        return None, dims
    if prov == "openai_compat":
        base = MEMORY_EMBED_BASE_URL or LLM_BASE_URL
        key = MEMORY_EMBED_API_KEY or LLM_API_KEY
        if not key:
            log.warning("embed.provider=openai_compat but no api_key "
                        "(embed.api_key / llm.api_key both empty); "
                        "memory degrades to FTS-only")
            return None, dims
        return OpenAICompatibleEmbeddingProvider(
            base, key, MEMORY_EMBED_MODEL, dims), dims
    # local(默认)
    try:
        import sentence_transformers  # noqa: F401  仅探可装性,不实例化
    except ImportError:
        log.error("embed.provider=local but sentence-transformers not installed; "
                  "memory degrades to FTS-only. "
                  "Fix: pip install -e \".[embed]\"")
        return None, dims
    if not Path(MEMORY_EMBED_MODEL_PATH).is_dir():
        log.error("embed.provider=local but model_path %r missing; "
                  "memory degrades to FTS-only. "
                  "Fix: download BAAI/%s to that path",
                  MEMORY_EMBED_MODEL_PATH, MEMORY_EMBED_MODEL)
        return None, dims
    return SentenceTransformerProvider(
        MEMORY_EMBED_MODEL_PATH, dims, MEMORY_EMBED_MODEL), dims


def get_memory_manager() -> MemoryManager:
    """进程单例(懒加载)。@tool 函数 + MemoryHook 调用此函数;测试通过
    _set_memory_manager 替换。懒 import config 避免导入时副作用。"""
    global _MEMORY_MANAGER
    if _MEMORY_MANAGER is None:
        from twinkle.config import (
            MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
            MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE,
            MEMORY_DIR,
            MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
            MEMORY_INDEX_DEBOUNCE_SECONDS,
            MEMORY_QUERY_MAX_RESULTS,
            MEMORY_WATCH_INTERVAL_SECONDS,
        )
        provider, dims = _build_embed_provider()
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
            enable_watcher=True,
            watch_interval_seconds=MEMORY_WATCH_INTERVAL_SECONDS)
        import atexit
        atexit.register(_MEMORY_MANAGER.close)
    return _MEMORY_MANAGER


def _set_memory_manager(mgr: MemoryManager | None) -> None:
    """测试钩子:替换/重置单例。生产代码从不调用此函数。
    替换时先 close 旧实例(停其 watchdog observer),防线程泄露。"""
    global _MEMORY_MANAGER
    old = _MEMORY_MANAGER
    if old is not None and old is not mgr:
        old.close()
    _MEMORY_MANAGER = mgr


__all__ = [
    "MemoryManager", "EmbeddingProvider", "MockEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "get_memory_manager", "_set_memory_manager",
]
