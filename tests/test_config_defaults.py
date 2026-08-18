"""config 默认值断言——per-invoke frozen prefix 后 memory auto-inject 默认开。"""


def test_memory_auto_inject_default_enabled():
    """Schema 默认 auto_inject.enabled=True(被动召回 USER.md/MEMORY.md 默认开)。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    assert MemoryAutoInjectConfig().enabled is True


def test_memory_auto_inject_max_chars_default():
    """max_chars 默认不变(12000)。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    assert MemoryAutoInjectConfig().max_chars == 12000
