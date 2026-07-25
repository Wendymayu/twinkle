"""Shared test fixtures — avoids depending on pytest-asyncio just for free ports."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port_factory():
    """Returns a callable that yields a free TCP port each call."""
    return _free_port


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def sessions_dir(tmp_path) -> "Path":
    """A fresh per-test sessions directory (disk-backed SessionStore target)."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def session_store(sessions_dir):
    """A SessionStore rooted in a per-test tmp dir (no repo pollution)."""
    from twinkle.agentserver.sessions import SessionStore
    return SessionStore(str(sessions_dir))


@pytest.fixture
def todos_dir(tmp_path) -> "Path":
    """A fresh per-test todos directory (disk-backed TodoStore target)."""
    d = tmp_path / "todos"
    d.mkdir()
    return d


@pytest.fixture
def todo_store(todos_dir):
    """A TodoStore rooted in a per-test tmp dir (no repo pollution)."""
    from twinkle.agentserver.todo.store import TodoStore
    return TodoStore(str(todos_dir))


@pytest.fixture
def isolated_todo_store(tmp_path):
    """Construct a tmp-backed TodoStore, set it as the process singleton (so
    get_todo_store() returns it during tests), yield it, and reset after.
    For tests that drive the todo tools or the agent loop's todo path."""
    from twinkle.agentserver.todo import _set_todo_store
    from twinkle.agentserver.todo.store import TodoStore
    s = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(s)
    yield s
    _set_todo_store(None)
