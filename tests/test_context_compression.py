import asyncio

from twinkle.agentserver.compression import (
    _render_messages_text,
    _STRUCTURED_SUMMARY_PROMPT,
    split_messages_head_middle_tail,
    _summarize,
    compress_messages,
    do_compress,
    estimate_tokens,
    should_compress,
)
from twinkle.agentserver.llm_client import Finish, TextDelta


class FakeLLM:
    """Minimal stub: yields the configured summary text as TextDelta, then a Finish."""

    def __init__(self, summary_text: str = "summary"):
        self.summary_text = summary_text
        self.calls: list = []

    async def stream(self, messages, tools):
        self.calls.append((messages, tools))
        yield TextDelta(self.summary_text)
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": self.summary_text, "tool_calls": None},
        )


# --- estimate_tokens ---
def test_estimate_tokens_empty():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_monotonic():
    small = [{"role": "user", "content": "hello world"}]  # 11 chars -> //3 = 3
    big = [{"role": "user", "content": "hi" * 100}]  # 200 chars -> //3 = 66
    assert 0 < estimate_tokens(small) < estimate_tokens(big)


def test_estimate_tokens_handles_content_list_and_tool_calls():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "abc"}]},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "web_fetch", "arguments": '{"url":"x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert estimate_tokens(msgs) > 0


# --- split_messages_head_middle_tail ---
def test_split_returns_all_when_small():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    head, middle, tail = split_messages_head_middle_tail(msgs, tail_count=4)
    assert head == [{"role": "system", "content": "s"}]
    assert middle == []
    assert tail == msgs


def test_split_head_is_system_middle_tail_split():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i}"} for i in range(10)]
    head, middle, tail = split_messages_head_middle_tail(msgs, tail_count=3)
    assert head == [{"role": "system", "content": "s"}]
    assert len(middle) == 7
    assert middle[0]["content"] == "u0"
    assert middle[-1]["content"] == "u6"
    assert [m["content"] for m in tail] == ["u7", "u8", "u9"]


def test_split_does_not_break_tool_pair():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i}"} for i in range(5)]
    msgs.append({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "r"})
    head, middle, tail = split_messages_head_middle_tail(msgs, tail_count=1)
    assert tail[0]["role"] == "assistant"
    assert tail[0].get("tool_calls") is not None
    assert tail[-1]["role"] == "tool"
    assert head == [{"role": "system", "content": "s"}]


# --- _render + _summarize ---
def test_render_messages_text_includes_roles_and_tool_calls():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
    ]
    text = _render_messages_text(msgs)
    assert "[user]" in text and "hello" in text
    assert "tool_call" in text and "t" in text


def test_summarize_collects_textdeltas(monkeypatch):
    monkeypatch.setattr("twinkle.agentserver.compression.CONTEXT_SUMMARY_PROMPT_MODE", "free")
    llm = FakeLLM(summary_text="the summary")
    out = asyncio.run(_summarize(llm, "sysprompt", "middle text"))
    assert out == "the summary"
    msgs, tools = llm.calls[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "sysprompt"
    assert msgs[1]["role"] == "user" and "middle text" in msgs[1]["content"]
    assert tools == []


def test_summarize_uses_structured_prompt_in_structured_mode(monkeypatch):
    monkeypatch.setattr("twinkle.agentserver.compression.CONTEXT_SUMMARY_PROMPT_MODE", "structured")
    llm = FakeLLM(summary_text="结构化摘要")
    asyncio.run(_summarize(llm, "free form prompt", "middle"))
    msgs, _ = llm.calls[0]
    # structured 模式用硬编码常量,非传入的 free form prompt
    assert msgs[0]["content"] == _STRUCTURED_SUMMARY_PROMPT


def test_structured_summary_prompt_has_four_sections():
    assert "关键事实与决定" in _STRUCTURED_SUMMARY_PROMPT
    assert "已用工具与文件" in _STRUCTURED_SUMMARY_PROMPT
    assert "待办与当前任务" in _STRUCTURED_SUMMARY_PROMPT
    assert "错误与修复" in _STRUCTURED_SUMMARY_PROMPT


# --- compress_messages ---
def test_compress_noop_under_threshold_returns_copy():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out = asyncio.run(compress_messages(
        msgs, FakeLLM(), token_threshold=10_000, keep_recent_pairs=6,
        summary_system_prompt="p"))
    assert out == msgs
    assert out is not msgs


def test_compress_over_threshold_summarizes_and_shrinks():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"turn {i} FACTKEY_{i} " + "x" * 30} for i in range(20)]
    msgs += [{"role": "assistant", "content": f"ans {i} FACTKEY_{i} " + "y" * 30} for i in range(20)]
    llm = FakeLLM(summary_text="摘要含 FACTKEY_5")
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=10, keep_recent_pairs=3, summary_system_prompt="p"))
    assert estimate_tokens(out) < estimate_tokens(msgs)
    assert out[0]["role"] == "system" and out[0]["content"] == "s"
    assert any("[prior context summary]" in m.get("content", "") for m in out)
    assert any("FACTKEY_19" in m.get("content", "") for m in out)
    assert any("FACTKEY_5" in m.get("content", "") for m in out)


def test_compress_tool_pair_intact_in_tail():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    msgs.append({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "r" * 30})
    out = asyncio.run(compress_messages(
        msgs, FakeLLM(), token_threshold=10, keep_recent_pairs=1, summary_system_prompt="p"))
    idx_tool = next(i for i, m in enumerate(out) if m.get("role") == "tool")
    assert out[idx_tool - 1].get("tool_calls") is not None


# --- e2e 100 turns (acceptance) ---
def test_e2e_100_turns_token_down_facts_preserved():
    msgs = [{"role": "system", "content": "sys prompt"}]
    for i in range(100):
        msgs.append({"role": "user", "content": f"第{i}轮 FACTKEY_{i:03d} 提问 " + "甲" * 40})
        msgs.append({"role": "assistant", "content": f"第{i}轮 FACTKEY_{i:03d} 回答 " + "乙" * 40})
    llm = FakeLLM(summary_text="历史摘要：提及 FACTKEY_050 与 FACTKEY_070")
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=100, keep_recent_pairs=6, summary_system_prompt="p"))
    assert estimate_tokens(out) < estimate_tokens(msgs)
    assert any("FACTKEY_099" in m.get("content", "") for m in out)
    assert any("FACTKEY_050" in m.get("content", "") for m in out)
    assert out[0]["content"] == "sys prompt"


class _RaisingLLM:
    """Stub whose stream() raises — simulates a transient summary LLM outage."""

    async def stream(self, messages, tools):
        raise RuntimeError("summary outage")
        yield  # unreachable; makes this an async generator


def test_compress_degrades_when_summary_fails():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    out = asyncio.run(compress_messages(
        msgs, _RaisingLLM(), token_threshold=10, keep_recent_pairs=3,
        summary_system_prompt="p"))
    # degraded: head + tail, no summary message, did not raise
    assert out[0]["role"] == "system"
    assert not any("[prior context summary]" in m.get("content", "") for m in out)
    assert estimate_tokens(out) < estimate_tokens(msgs)  # still shrunk (middle dropped)


# --- should_compress ---
def test_should_compress_false_under_threshold():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert should_compress(msgs, token_threshold=10_000, keep_recent_pairs=6) is False


def test_should_compress_false_when_tail_eats_all_middle():
    # tail_count (12) >= len(rest) (1) -> no middle even though over threshold
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert should_compress(msgs, token_threshold=1, keep_recent_pairs=6) is False


def test_should_compress_true_over_threshold_with_middle():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    assert should_compress(msgs, token_threshold=10, keep_recent_pairs=3) is True


# --- do_compress ---
def test_do_compress_summarizes_when_called_directly():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    out = asyncio.run(do_compress(
        msgs, FakeLLM(summary_text="摘要含 FACTKEY_5"),
        keep_recent_pairs=3, summary_system_prompt="p"))
    assert any("[prior context summary]" in m.get("content", "") for m in out)
    assert estimate_tokens(out) < estimate_tokens(msgs)


def test_do_compress_degrades_when_summary_fails():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user", "content": f"u{i} " + "x" * 30} for i in range(20)]
    out = asyncio.run(do_compress(
        msgs, _RaisingLLM(), keep_recent_pairs=3, summary_system_prompt="p"))
    assert not any("[prior context summary]" in m.get("content", "") for m in out)
    assert estimate_tokens(out) < estimate_tokens(msgs)  # middle dropped
