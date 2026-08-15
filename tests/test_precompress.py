from twinkle.agentserver.compression import _tool_result_budget


def _tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_call(call_id, name="read_file"):
    return {"role": "assistant", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": "{}"}}]}


def test_tool_result_budget_noop_under_threshold():
    msgs = [{"role": "system", "content": "s"},
            _assistant_call("c1"), _tool_msg("c1", "x" * 100)]
    out = _tool_result_budget(msgs)
    assert out == msgs
    assert out is not msgs


def test_tool_result_budget_trims_largest_when_over_threshold():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(3):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, "大结果" * 5000))  # ~20000 字符=6666 token
    out = _tool_result_budget(msgs)
    trimmed = [m for m in out
               if m.get("role") == "tool" and "[...trimmed" in m.get("content", "")]
    assert len(trimmed) == 1
    assert len(trimmed[0]["content"]) < 3200
    # lossless: 输入 msgs 未被改写(原文仍在,只改"发 LLM 这份" out)
    assert all("大结果" * 5000 in m.get("content", "")
               for m in msgs if m.get("role") == "tool")


def test_tool_result_budget_protects_latest_tool_result():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(3):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, "独占内容_" + str(i) + "_" + "X" * 20000))
    out = _tool_result_budget(msgs)
    # 最新一条(c2,含"独占内容_2")必须豁免——未被 trim
    latest = [m for m in out
              if m.get("role") == "tool" and "独占内容_2" in m.get("content", "")]
    assert len(latest) == 1
    assert "[...trimmed" not in latest[0]["content"]
    # 至少一条更旧的被 trim
    assert any("[...trimmed" in m.get("content", "")
               for m in out if m.get("role") == "tool")


from twinkle.agentserver.compression import _micro_compact


def test_micro_compact_clears_old_same_tool_results():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    contents = [m["content"] for m in tools]
    markers = [c for c in contents if c == "[Old tool result content cleared]"]
    # 10 条:可清 7 > 5(trigger) → 清 7,留最近 3
    assert len(markers) == 7
    assert contents[-3:] == ["result_7", "result_8", "result_9"]
    # lossless: 输入 msgs 10 条原文全在,未被 marker 污染
    assert all(m["content"].startswith("result_")
               for m in msgs if m.get("role") == "tool")


def test_micro_compact_not_triggered_at_boundary_8():
    # 8 条:可清 8-3=5,5 不 >5 → 不清
    msgs = [{"role": "system", "content": "s"}]
    for i in range(8):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    assert all(m["content"].startswith("result_") for m in tools)
    assert not any(m["content"] == "[Old tool result content cleared]" for m in tools)


def test_micro_compact_clears_at_boundary_9():
    # 9 条:可清 6 > 5 → 清 6,留 3
    msgs = [{"role": "system", "content": "s"}]
    for i in range(9):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    markers = [m for m in out
               if m.get("role") == "tool" and m["content"] == "[Old tool result content cleared]"]
    assert len(markers) == 6


def test_micro_compact_skips_non_compactable_tools():
    # list_skill 不在 compactable 名单 → 即使积 10 条也不清
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        msgs.append(_assistant_call(cid, name="list_skill"))
        msgs.append(_tool_msg(cid, f"result_{i}"))
    out = _micro_compact(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    assert all(m["content"].startswith("result_") for m in tools)


import asyncio
from twinkle.agentserver.compression import precompress_messages, compress_messages
from twinkle.agentserver.llm_client import TextDelta, Finish


class _RecordingLLM:
    """Counts stream() calls — 0 means LLM summary was skipped."""
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        yield TextDelta("摘要")
        yield Finish(finish_reason="stop",
                     assistant_message={"role": "assistant", "content": "摘要", "tool_calls": None})


def test_precompress_runs_budget_then_micro_compact():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(10):
        cid = f"c{i}"
        # c0 用 list_skill(非 compactable)→ budget 仍 trim(按大小不按工具名),
        # 但 micro_compact 跳过它 → trimmed 内容存活;其余 9 条 read_file 被 micro_compact 清
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "list_skill" if i == 0 else "read_file", "arguments": "{}"}}]})
        msgs.append(_tool_msg(cid, "X" * 30000 if i == 0 else f"r{i}"))
    out = precompress_messages(msgs)
    # 最早的大 result(c0)被 ToolResultBudget trim
    assert any("[...trimmed" in m.get("content", "")
               for m in out if m.get("role") == "tool")
    # 其余旧 read_file 结果被 MicroCompact 清成 marker(留最近 3)
    assert any(m["content"] == "[Old tool result content cleared]"
               for m in out if m.get("role") == "tool")


def test_compress_messages_precompress_saves_llm_when_below_threshold():
    # 24 条 read_file,每条 ~2666 token,总量 64000 > 60000(压前会触发压缩)
    # precompress 清 21 条成 marker后 token 骤降 → should_compress=false → 不调 LLM
    msgs = [{"role": "system", "content": "s"}]
    for i in range(24):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append(_tool_msg(cid, "块" * 8000))  # 8000 字符=2666 token
    llm = _RecordingLLM()
    out = asyncio.run(compress_messages(
        msgs, llm, token_threshold=60000, keep_recent_pairs=3,
        summary_system_prompt="p"))
    assert llm.calls == 0  # precompress 后降到阈值下 → 省 LLM 摘要
    tools = [m for m in out if m.get("role") == "tool"]
    assert any("块" in m["content"] for m in tools[-3:])  # 最近 3 原文保留
    assert any(m["content"] == "[Old tool result content cleared]" for m in tools)
