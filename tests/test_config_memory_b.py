from twinkle.config.schema import (
    MemoryFlushConfig,
    MemoryDreamingConfig,
    MemoryConfig,
)


def test_flush_defaults_off():
    c = MemoryFlushConfig()
    assert c.enabled is False


def test_dreaming_defaults_off():
    c = MemoryDreamingConfig()
    assert c.enabled is False
    assert c.interval_seconds == 3600
    assert c.start_delay_seconds == 300
    assert c.top_k == 5


def test_memory_config_has_flush_dreaming():
    c = MemoryConfig()
    assert isinstance(c.flush, MemoryFlushConfig)
    assert isinstance(c.dreaming, MemoryDreamingConfig)


def test_unknown_key_rejected():
    import pytest
    with pytest.raises(Exception):
        MemoryFlushConfig(bogus=1)
