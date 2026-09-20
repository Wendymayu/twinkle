import pytest
from pydantic import ValidationError

from twinkle.config import (
    MEMORY_CHUNKING_OVERLAP, MEMORY_CHUNKING_TOKENS,
    MEMORY_CLEANUP_MAX_CHUNKS_PER_FILE, MEMORY_DIR,
    MEMORY_EMBED_API_KEY, MEMORY_EMBED_BASE_URL,
    MEMORY_EMBED_DIMS, MEMORY_EMBED_MODEL,
    MEMORY_EMBED_MODEL_PATH, MEMORY_EMBED_PROVIDER,
    MEMORY_HYBRID_CANDIDATE_MULTIPLIER,
    MEMORY_HYBRID_TEXT_WEIGHT, MEMORY_HYBRID_VECTOR_WEIGHT,
    MEMORY_QUERY_MAX_RESULTS,
)
from twinkle.config import settings


def test_memory_constants_flattened():
    assert MEMORY_EMBED_MODEL == "bge-small-zh-v1.5"
    assert MEMORY_EMBED_PROVIDER == "local"
    assert MEMORY_EMBED_DIMS == 512
    assert MEMORY_EMBED_MODEL_PATH.replace("\\", "/").endswith(
        "models/bge-small-zh-v1.5")
    assert MEMORY_EMBED_BASE_URL == ""
    assert MEMORY_EMBED_API_KEY == ""
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
    for t in ("memory_search", "write_memory", "read_memory"):
        assert tools.get(t) == "allow", f"{t} not allow in default permissions"


def test_strict_model_rejects_unknown_memory_key():
    from twinkle.config.schema import MemoryConfig
    with pytest.raises(ValidationError):
        MemoryConfig(totallyboguskey=1)


def test_embed_subsection_defaults_and_literals():
    from twinkle.config.schema import MemoryEmbedConfig
    e = MemoryEmbedConfig()
    assert e.provider == "local"
    assert e.model == "bge-small-zh-v1.5"
    assert e.model_path == "models/bge-small-zh-v1.5"
    assert e.dims == 512
    assert e.base_url == "" and e.api_key == ""
    with pytest.raises(ValidationError):
        MemoryEmbedConfig(provider="bogus")  # Literal 取值域拦截
