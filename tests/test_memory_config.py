from twinkle.config import (
    MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
    MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE, MEMORY_DIR,
    MEMORY_EMBED_MODEL, MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
    MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
    MEMORY_QUERY_MAX_RESULTS,
)
from twinkle.config import settings


def test_memory_constants_flattened():
    assert MEMORY_EMBED_MODEL == "text-embedding-3-small"
    assert MEMORY_QUERY_MAX_RESULTS == 10
    assert MEMORY_HYBRID_VECTOR_WEIGHT == 0.7
    assert MEMORY_HYBRID_TEXT_WEIGHT == 0.3
    assert MEMORY_HYBRID_CANDIDATE_MULTIPLIER == 2.0
    assert MEMORY_CHUNKING_TOKENS == 256
    assert MEMORY_CHUNKING_OVERLAP == 32
    assert MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE == 200
    assert MEMORY_DIR.replace("\\", "/").endswith(".twinkle_data/memory")


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
