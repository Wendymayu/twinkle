import asyncio

from twinkle.agentserver.tools.builtin.subagent.context import (
    SUBAGENT_EXECUTOR, SUBAGENT_PARENT_REQUEST_ID, SUBAGENT_PARENT_SESSION_ID,
)
from twinkle.agentserver.tools.builtin.subagent import SubagentResult


class _FakeExecutor:
    """Captures the task + parent ids; returns a canned result."""
    def __init__(self, result):
        self._result = result
        self.captured = []
    async def execute_subagent(self, task, parent_session_id, parent_request_id):
        self.captured.append((task, parent_session_id, parent_request_id))
        return self._result


def test_spawn_subagent_returns_final_plus_stop_hint(session_store):
    from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
    fake = _FakeExecutor(SubagentResult(success=True,
                                        result="the answer"))
    tok_e = SUBAGENT_EXECUTOR.set(fake)
    tok_s = SUBAGENT_PARENT_SESSION_ID.set("p1")
    tok_r = SUBAGENT_PARENT_REQUEST_ID.set("r1")
    try:
        out = asyncio.run(spawn_subagent.invoke(
            {"objective": "do X", "prompt": ""}))
    finally:
        SUBAGENT_EXECUTOR.reset(tok_e)
        SUBAGENT_PARENT_SESSION_ID.reset(tok_s)
        SUBAGENT_PARENT_REQUEST_ID.reset(tok_r)
    assert "the answer" in out
    assert "[SYSTEM]" in out
    assert "Do NOT call spawn_subagent again" in out
    # executor received the right parent ids from ContextVar
    assert fake.captured[0][1] == "p1"
    assert fake.captured[0][2] == "r1"


def test_spawn_subagent_failure_returns_error_plus_stop_hint(session_store):
    from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
    fake = _FakeExecutor(SubagentResult(success=False,
                                        error="boom"))
    tok_e = SUBAGENT_EXECUTOR.set(fake)
    tok_s = SUBAGENT_PARENT_SESSION_ID.set("p1")
    tok_r = SUBAGENT_PARENT_REQUEST_ID.set("r1")
    try:
        out = asyncio.run(spawn_subagent.invoke(
            {"objective": "do X", "prompt": ""}))
    finally:
        SUBAGENT_EXECUTOR.reset(tok_e)
        SUBAGENT_PARENT_SESSION_ID.reset(tok_s)
        SUBAGENT_PARENT_REQUEST_ID.reset(tok_r)
    assert "boom" in out
    assert "[SYSTEM]" in out


def test_spawn_subagent_no_executor_returns_unavailable():
    from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
    # no ContextVar set -> graceful string, no raise
    out = asyncio.run(spawn_subagent.invoke(
        {"objective": "x", "prompt": ""}))
    assert "unavailable" in out.lower()


def test_spawn_subagent_is_a_streaming_free_tool():
    from twinkle.agentserver.tools.base import Tool
    from twinkle.agentserver.tools.builtin.subagent import spawn_subagent
    assert isinstance(spawn_subagent, Tool)
    assert spawn_subagent.card.name == "spawn_subagent"
    # schema is auto-derived from the signature
    params = spawn_subagent.card.parameters
    assert "objective" in params.get("required", [])
