from twinkle import config


def test_context_defaults_present():
    assert isinstance(config.CONTEXT_TOKEN_THRESHOLD, int)
    assert isinstance(config.CONTEXT_KEEP_RECENT_PAIRS, int)
    assert config.CONTEXT_TOKEN_THRESHOLD == 0  # 0=动态(窗口×trigger_ratio,默认)
    assert config.CONTEXT_KEEP_RECENT_PAIRS > 0
    assert isinstance(config.CONTEXT_SUMMARY_PROMPT, str) and config.CONTEXT_SUMMARY_PROMPT


def test_todos_dir_default_present():
    from pathlib import Path
    assert isinstance(config.TODOS_DIR, str) and config.TODOS_DIR
    p = Path(config.TODOS_DIR)
    assert p.name == "todos"
    assert p.parent.name == ".twinkle_data"
