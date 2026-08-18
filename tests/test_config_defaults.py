"""config 默认值断言——per-invoke frozen prefix 后 memory auto-inject 默认开。"""


def test_memory_auto_inject_default_enabled():
    """Schema 默认 auto_inject.enabled=True(被动召回 USER.md/MEMORY.md 默认开)。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    assert MemoryAutoInjectConfig().enabled is True


def test_memory_auto_inject_budget_defaults():
    """分预算默认:USER.md 4000(对齐 openclaw USER_BOOTSTRAP_MAX_CHARS)/MEMORY.md 12000。"""
    from twinkle.config.schema import MemoryAutoInjectConfig
    cfg = MemoryAutoInjectConfig()
    assert cfg.max_chars_user == 4000
    assert cfg.max_chars_memory == 12000
