"""memory 包 —— 重新导出 + 进程级单例(对齐 skills/__init__)。"""
from twinkle.agentserver.memory.store import MemoryManager
from twinkle.agentserver.memory.embeddings import (
    EmbeddingProvider,
    MockEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)

_MEMORY_MANAGER: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    """进程单例(懒加载)。@tool 函数 + MemoryHook 调用此函数;测试通过
    _set_memory_manager 替换。懒 import config 避免导入时副作用。"""
    global _MEMORY_MANAGER
    if _MEMORY_MANAGER is None:
        from twinkle.config import (
            LLM_API_KEY, LLM_BASE_URL,
            MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
            MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE,
            MEMORY_DIR, MEMORY_EMBED_MODEL,
            MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
            MEMORY_INDEX_DEBOUNCE_SECONDS,
            MEMORY_QUERY_MAX_RESULTS,
        )
        provider = None
        dims = 1536  # 对齐 text-embedding-3-small;改 model + dims 后需删 memory.db
        if LLM_API_KEY:
            provider = OpenAICompatibleEmbeddingProvider(
                LLM_BASE_URL, LLM_API_KEY, MEMORY_EMBED_MODEL, dims)
        _MEMORY_MANAGER = MemoryManager(
            MEMORY_DIR, provider, dims=dims,
            chunk_tokens=MEMORY_CHUNKING_TOKENS,
            chunk_overlap=MEMORY_CHUNKING_OVERLAP,
            max_results=MEMORY_QUERY_MAX_RESULTS,
            vector_weight=MEMORY_HYBRID_VECTOR_WEIGHT,
            text_weight=MEMORY_HYBRID_TEXT_WEIGHT,
            candidate_multiplier=MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
            max_chunks_per_file=MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE,
            index_debounce_seconds=MEMORY_INDEX_DEBOUNCE_SECONDS)
    return _MEMORY_MANAGER


def _set_memory_manager(mgr: MemoryManager | None) -> None:
    """测试钩子:替换/重置单例。生产代码从不调用此函数。"""
    global _MEMORY_MANAGER
    _MEMORY_MANAGER = mgr


__all__ = [
    "MemoryManager", "EmbeddingProvider", "MockEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "get_memory_manager", "_set_memory_manager",
]
