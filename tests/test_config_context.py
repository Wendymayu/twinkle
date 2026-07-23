from twinkle import config


def test_context_defaults_present():
    assert isinstance(config.CONTEXT_TOKEN_THRESHOLD, int)
    assert isinstance(config.CONTEXT_KEEP_RECENT_PAIRS, int)
    assert config.CONTEXT_TOKEN_THRESHOLD > 0
    assert config.CONTEXT_KEEP_RECENT_PAIRS > 0
    assert isinstance(config.CONTEXT_SUMMARY_PROMPT, str) and config.CONTEXT_SUMMARY_PROMPT
