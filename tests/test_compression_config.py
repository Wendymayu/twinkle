from twinkle.config import (
    settings,
    CONTEXT_SUMMARY_PROMPT_MODE,
    MICRO_COMPACT_TRIGGER_THRESHOLD,
    MICRO_COMPACT_KEEP_RECENT_PER_TOOL,
    MICRO_COMPACT_COMPACTABLE_TOOL_NAMES,
    MICRO_COMPACT_CLEARED_MARKER,
    TOOL_RESULT_BUDGET_TOKENS_THRESHOLD,
    TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD,
    TOOL_RESULT_BUDGET_TRIM_SIZE,
    TOOL_RESULT_BUDGET_PROTECT_LATEST,
)


def test_compression_config_defaults():
    assert CONTEXT_SUMMARY_PROMPT_MODE == "structured"
    assert MICRO_COMPACT_TRIGGER_THRESHOLD == 5
    assert MICRO_COMPACT_KEEP_RECENT_PER_TOOL == 3
    assert MICRO_COMPACT_COMPACTABLE_TOOL_NAMES == [
        "read_file", "glob", "command_exec", "web_fetch", "web_search"]
    assert MICRO_COMPACT_CLEARED_MARKER == "[Old tool result content cleared]"
    assert TOOL_RESULT_BUDGET_TOKENS_THRESHOLD == 9000
    assert TOOL_RESULT_BUDGET_LARGE_MESSAGE_THRESHOLD == 3000
    assert TOOL_RESULT_BUDGET_TRIM_SIZE == 3000
    assert TOOL_RESULT_BUDGET_PROTECT_LATEST == 1


def test_compression_config_nested_models_loaded():
    assert settings.context_compression.micro_compact.keep_recent_per_tool == 3
    assert settings.context_compression.tool_result_budget.protect_latest == 1
    assert settings.context_compression.summary_prompt_mode == "structured"
