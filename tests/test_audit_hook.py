"""Tests for AuditHook — always-on 工具执行审计(success/denied/error + 截断)。

不依赖 pytest-asyncio:用 asyncio.run() + tmp_path,对齐项目 conftest 风格。
直接构造 HookContext 调 hook 方法,避开完整 agent loop。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from twinkle.agentserver.hooks.base import HookContext, HookEvent, ToolCallInputs
from twinkle.agentserver.hooks.builtin.audit_hook import AuditHook
from twinkle.agentserver.hooks.decorator import hook
from twinkle.agentserver.hooks.manager import HookManager
from twinkle.agentserver.tools.errors import ToolError


class _FakeAgent:
    """Minimal agent shell exposing _hook_manager (what @hook reads)."""

    def __init__(self, hm: HookManager):
        self._hook_manager = hm


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ctx(extra: dict | None = None, force_finish: bool = False) -> HookContext:
    ctx = HookContext(
        agent=None,
        event=HookEvent.AFTER_TOOL_CALL,
        inputs=ToolCallInputs(name="echo", args={"text": "hi"}, tool_call_id="tc1"),
        session_id="sess-1",
        request_id="req-1",
        extra=dict(extra or {}),
    )
    if force_finish:
        ctx.request_force_finish()
    return ctx


def _hook(tmp_path: Path, **kw) -> AuditHook:
    return AuditHook(path=str(tmp_path / "tool_audit.jsonl"), **kw)


def test_success_writes_outcome_and_result(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx(extra={"_tool_result": "hello world"})
    asyncio.run(hook.after_tool_call(ctx))

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "echo"
    assert row["outcome"] == "success"
    assert row["result"] == "hello world"
    assert json.loads(row["args"]) == {"text": "hi"}
    assert row["tool_call_id"] == "tc1"
    assert row["session_id"] == "sess-1"
    assert row["request_id"] == "req-1"
    assert "ts" in row


def test_before_captures_force_finish_as_denied(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx(force_finish=True)  # 高 priority hook 已 request_force_finish;body 将被 @hook 跳过
    asyncio.run(hook.before_tool_call(ctx))

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["tool"] == "echo"
    assert rows[0]["result"] == ""


def test_before_no_op_when_no_force_finish(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx()  # 未被 force_finish → 工具将正常执行,after 会记 success
    asyncio.run(hook.before_tool_call(ctx))

    assert _read_jsonl(tmp_path / "tool_audit.jsonl") == []


def test_on_tool_exception_records_tool_error_kind(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx()
    ctx.exception = ToolError("blocked for safety", kind="denied")
    asyncio.run(hook.on_tool_exception(ctx))

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["error"] == "blocked for safety"


def test_on_tool_exception_records_generic_error(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx()
    ctx.exception = ValueError("boom")
    asyncio.run(hook.on_tool_exception(ctx))

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error"] == "boom"


def test_truncates_long_args_and_result(tmp_path):
    hook = _hook(tmp_path, max_arg_chars=10, max_result_chars=5)
    ctx = _ctx(extra={"_tool_result": "x" * 100})
    ctx.inputs = ToolCallInputs(
        name="echo", args={"text": "y" * 100}, tool_call_id="tc1")
    asyncio.run(hook.after_tool_call(ctx))

    row = _read_jsonl(tmp_path / "tool_audit.jsonl")[0]
    assert len(row["result"]) <= len("x" * 5 + "...[truncated]")
    assert row["result"].endswith("...[truncated]")
    assert len(row["args"]) <= len("y" * 10 + "...[truncated]")
    assert row["args"].endswith("...[truncated]")


def test_disabled_writes_nothing(tmp_path):
    hook = _hook(tmp_path, enabled=False)
    ctx = _ctx(extra={"_tool_result": "out"})
    asyncio.run(hook.after_tool_call(ctx))
    asyncio.run(hook.on_tool_exception(_ctx()))

    assert _read_jsonl(tmp_path / "tool_audit.jsonl") == []


def test_non_serializable_args_does_not_raise(tmp_path):
    hook = _hook(tmp_path)
    ctx = _ctx()
    ctx.inputs = ToolCallInputs(
        name="echo", args={"obj": object()}, tool_call_id="tc1")
    # default=str fallback handles non-serializable args
    asyncio.run(hook.after_tool_call(ctx))

    row = _read_jsonl(tmp_path / "tool_audit.jsonl")[0]
    assert row["outcome"] == "success"
    assert "object" in row["args"]


def test_integration_through_real_hook_decorator(tmp_path):
    """End-to-end: AuditHook registered in a HookManager; a @hook-decorated
    _tool_call writes _tool_result (decorator) which AuditHook.after reads and
    persists. On exception, on_tool_exception fires and AuditHook records it."""
    audit = _hook(tmp_path)
    agent = _FakeAgent(HookManager())
    agent._hook_manager.register_hook(audit)

    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def tool_call(self, ctx):
        return "real-output"

    ctx = _ctx(extra={})
    result = asyncio.run(tool_call(agent, ctx))
    assert result == "real-output"

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["result"] == "real-output"

    # exception path: on_tool_exception fires, decorator sets ctx.exception
    @hook(HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL,
          on_exception=HookEvent.ON_TOOL_EXCEPTION)
    async def failing_tool(self, ctx):
        raise ToolError("nope", kind="failed")

    ctx2 = _ctx(extra={})
    try:
        asyncio.run(failing_tool(agent, ctx2))
    except ToolError:
        pass

    rows = _read_jsonl(tmp_path / "tool_audit.jsonl")
    assert len(rows) == 2
    assert rows[1]["outcome"] == "failed"
    assert rows[1]["error"] == "nope"
