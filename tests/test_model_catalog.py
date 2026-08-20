from twinkle.config.model_catalog import (
    MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW_TOKENS,
    normalize_model, resolve_context_window_limit,
)


def test_normalize_strips_tag_suffix():
    assert normalize_model("gpt-4o-mini:latest") == "gpt-4o-mini"
    assert normalize_model("GPT-4o-Mini") == "gpt-4o-mini"
    assert normalize_model("gpt-4o-mini") == "gpt-4o-mini"


def test_manual_override_wins():
    assert resolve_context_window_limit(
        model="gpt-4o-mini", manual_override=200_000) == 200_000


def test_dict_prefix_match_longest_key():
    assert resolve_context_window_limit(
        model="gpt-4o-mini-2024-07-18", manual_override=0) == 128_000


def test_dict_match_claude_dated():
    assert resolve_context_window_limit(
        model="claude-3-5-sonnet-20240129", manual_override=0) == 200_000


def test_unknown_model_falls_back_to_default():
    assert resolve_context_window_limit(
        model="some-unknown-model-x", manual_override=0) == DEFAULT_CONTEXT_WINDOW_TOKENS == 128_000


def test_default_token_value():
    assert DEFAULT_CONTEXT_WINDOW_TOKENS == 128_000


def test_dict_prefix_match_picks_longest_key(monkeypatch):
    """longest-key branch actually picks the longest matching key, not any match.
    Synthetic catalog where gpt-4o (100) and gpt-4o-mini (200) differ — only
    picking gpt-4o-mini (longest) returns 200."""
    import twinkle.config.model_catalog as mc
    monkeypatch.setattr(mc, "MODEL_CONTEXT_WINDOWS",
        {"gpt-4o": 100, "gpt-4o-mini": 200})
    assert resolve_context_window_limit(
        model="gpt-4o-mini-2024-07-18", manual_override=0) == 200
