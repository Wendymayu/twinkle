"""Tests for _merge_system_messages — ensures identity-first ordering
and correct handling of edge cases (single system, no system, mixed)."""

from twinkle.agentserver.agent_loop import AgentLoop


def _msg(role, content):
    return {"role": role, "content": content}


# --- Merge with multiple system messages (the main case) --- #

def test_merge_orders_identity_before_skill_and_memory():
    """Identity section should appear first in the merged content,
    skill second, memory third — matching jiuwenswarm's priority ordering."""
    messages = [
        _msg("system", "## 长期记忆\n你有跨会话记忆..."),          # MemoryHook prepend
        _msg("system", "## 可用技能\n1. doc-audit: ..."),          # SkillHook prepend
        _msg("system", "# 身份与行为原则\n对外交流时..."),          # SYSTEM_PROMPT
        _msg("user", "你好"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    assert len(result) == 2  # one merged system + one user
    assert result[0]["role"] == "system"
    content = result[0]["content"]
    # Identity first
    assert content.startswith("# 身份与行为原则")
    # Skill after identity
    idx_skill = content.index("## 可用技能")
    idx_identity = content.index("# 身份与行为原则")
    assert idx_skill > idx_identity
    # Memory after skill
    idx_memory = content.index("## 长期记忆")
    assert idx_memory > idx_skill


def test_merge_puts_compression_summary_after_memory():
    """Compression summary should come after skill/memory sections
    (closer to conversation for recency bias)."""
    messages = [
        _msg("system", "## 长期记忆\n记忆策略..."),
        _msg("system", "## 可用技能\n技能清单..."),
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("system", "[prior context summary] 用户之前要求..."),
        _msg("user", "继续上次"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    content = result[0]["content"]
    idx_summary = content.index("[prior context summary]")
    idx_memory = content.index("## 长期记忆")
    assert idx_summary > idx_memory


def test_merge_preserves_non_system_messages():
    """Conversation messages after system section should be untouched."""
    messages = [
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("system", "## 可用技能\n技能..."),
        _msg("user", "你好"),
        _msg("assistant", "嗨"),
        _msg("user", "帮我写代码"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    assert len(result) == 4  # 1 merged system + 3 conversation messages
    assert result[1]["role"] == "user"
    assert result[1]["content"] == "你好"
    assert result[2]["role"] == "assistant"


# --- Edge cases --- #

def test_merge_single_system_no_change():
    """If only one system message, no merge needed — return unchanged."""
    messages = [
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("user", "你好"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    assert len(result) == 2
    assert result[0]["content"] == "# 身份与行为原则\n身份..."


def test_merge_no_system_no_change():
    """If no system messages at all, return unchanged."""
    messages = [
        _msg("user", "你好"),
        _msg("assistant", "嗨"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    assert result == messages


def test_merge_empty_list():
    """Empty message list should return empty."""
    result = AgentLoop._merge_system_messages([])
    assert result == []


def test_merge_auto_list_skill_hint():
    """SkillHook's 'auto_list' mode injects "你有 skills 可用",
    which should also be classified as a skill section."""
    messages = [
        _msg("system", "你有 skills 可用。需要时先调 list_skill 看清单..."),
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("user", "你好"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    content = result[0]["content"]
    assert content.startswith("# 身份与行为原则")
    idx_skill = content.index("你有 skills 可用")
    idx_identity = content.index("# 身份与行为原则")
    assert idx_skill > idx_identity


def test_merge_unknown_system_section_preserved():
    """Unknown system message prefixes should be appended last."""
    messages = [
        _msg("system", "## 长期记忆\n记忆策略..."),
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("system", "## some new section\ncontent..."),
        _msg("user", "你好"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    content = result[0]["content"]
    # Unknown section appears after identity, skill, memory, summary
    idx_unknown = content.index("## some new section")
    idx_identity = content.index("# 身份与行为原则")
    assert idx_unknown > idx_identity


def test_merge_system_messages_not_at_head_unchanged():
    """System messages that appear AFTER non-system messages (e.g. a
    tool-result sandwiched between two system injections) should NOT
    be merged — only consecutive leading system messages are merged."""
    messages = [
        _msg("system", "# 身份与行为原则\n身份..."),
        _msg("user", "你好"),
        _msg("system", "## 可用技能\n技能..."),  # system after non-system — not at head
        _msg("assistant", "嗨"),
    ]
    result = AgentLoop._merge_system_messages(messages)
    # Only the first system message is at head — no merge needed (single system at head)
    assert len(result) == 4
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "# 身份与行为原则\n身份..."
    assert result[2]["role"] == "system"
