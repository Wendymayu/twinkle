"""Tests for approval persistence (Phase 10 — HITL 中断/恢复).

Verifies that ApprovalRegistry.save_pending / clear_pending / get_pending /
clear_all_pending correctly persist approval state to disk so that a browser
reconnection can recover pending approval cards.
"""
import asyncio
import json
import time
from pathlib import Path

from twinkle.agentserver.permissions.approval_registry import (
    APPROVAL_REGISTRY,
    ApprovalPendingRecord,
)


def _make_record(approval_id: str = "test-approval-id", session_id: str = "sess_test") -> ApprovalPendingRecord:
    return ApprovalPendingRecord(
        approval_id=approval_id,
        tool="command_exec",
        args={"command": "rm -rf /"},
        tool_call_id="tc_123",
        reason="requires approval",
        request_id="req_abc",
        session_id=session_id,
        created_at=time.time(),
    )


def test_save_pending_writes_file(tmp_path, monkeypatch):
    """save_pending writes .approval_pending.json to the session dir."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"
    record = _make_record(session_id=session_id)

    APPROVAL_REGISTRY.save_pending(session_id, record)

    path = tmp_path / session_id / ".approval_pending.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["approval_id"] == "test-approval-id"
    assert data[0]["tool"] == "command_exec"


def test_get_pending_returns_saved(tmp_path, monkeypatch):
    """get_pending reads back what was saved."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"
    record = _make_record(session_id=session_id)

    APPROVAL_REGISTRY.save_pending(session_id, record)
    pending = APPROVAL_REGISTRY.get_pending(session_id)

    assert len(pending) == 1
    assert pending[0]["approval_id"] == "test-approval-id"


def test_clear_pending_removes_entry(tmp_path, monkeypatch):
    """clear_pending removes the specific approval from the file."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"
    record = _make_record(session_id=session_id)

    APPROVAL_REGISTRY.save_pending(session_id, record)
    APPROVAL_REGISTRY.clear_pending(session_id, "test-approval-id")

    pending = APPROVAL_REGISTRY.get_pending(session_id)
    assert len(pending) == 0
    # File should be removed when empty
    path = tmp_path / session_id / ".approval_pending.json"
    assert not path.is_file()


def test_clear_pending_keeps_other_records(tmp_path, monkeypatch):
    """clear_pending only removes the targeted approval_id, not others."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"

    APPROVAL_REGISTRY.save_pending(session_id, _make_record("id-1", session_id))
    APPROVAL_REGISTRY.save_pending(session_id, _make_record("id-2", session_id))
    APPROVAL_REGISTRY.clear_pending(session_id, "id-1")

    pending = APPROVAL_REGISTRY.get_pending(session_id)
    assert len(pending) == 1
    assert pending[0]["approval_id"] == "id-2"


def test_clear_all_pending_removes_file(tmp_path, monkeypatch):
    """clear_all_pending removes the entire pending file."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"
    APPROVAL_REGISTRY.save_pending(session_id, _make_record(session_id=session_id))

    APPROVAL_REGISTRY.clear_all_pending(session_id)

    path = tmp_path / session_id / ".approval_pending.json"
    assert not path.is_file()


def test_get_pending_empty_session(tmp_path, monkeypatch):
    """get_pending returns [] for a session with no pending file."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    pending = APPROVAL_REGISTRY.get_pending("sess_nonexistent")
    assert pending == []


def test_get_pending_corrupt_file(tmp_path, monkeypatch):
    """get_pending returns [] for a corrupt .approval_pending.json."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_test"
    session_dir = tmp_path / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / ".approval_pending.json").write_text("NOT JSON", encoding="utf-8")

    pending = APPROVAL_REGISTRY.get_pending(session_id)
    assert pending == []


def test_save_pending_creates_session_dir(tmp_path, monkeypatch):
    """save_pending creates the session dir if it doesn't exist."""
    monkeypatch.setattr("twinkle.config.SESSIONS_DIR", str(tmp_path))
    session_id = "sess_new"
    record = _make_record(session_id=session_id)

    APPROVAL_REGISTRY.save_pending(session_id, record)

    assert (tmp_path / session_id).is_dir()
    assert (tmp_path / session_id / ".approval_pending.json").is_file()
