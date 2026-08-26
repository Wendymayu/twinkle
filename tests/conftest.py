"""共享测试 fixture —— 避免仅为 free port 而依赖 pytest-asyncio。"""
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
    """返回一个 callable，每次调用产出一个空闲 TCP port。"""
    return _free_port


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def sessions_dir(tmp_path) -> "Path":
    """每个测试一个全新的 sessions 目录（disk-backed SessionStore 目标）。"""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def session_store(sessions_dir):
    """以每个测试的 tmp dir 为根的 SessionStore（不污染仓库）。"""
    from twinkle.agentserver.sessions import SessionStore
    return SessionStore(str(sessions_dir))


@pytest.fixture
def todos_dir(tmp_path) -> "Path":
    """每个测试一个全新的 todos 目录（disk-backed TodoStore 目标）。"""
    d = tmp_path / "todos"
    d.mkdir()
    return d


@pytest.fixture
def todo_store(todos_dir):
    """以每个测试的 tmp dir 为根的 TodoStore（不污染仓库）。"""
    from twinkle.agentserver.todo.store import TodoStore
    return TodoStore(str(todos_dir))


@pytest.fixture
def isolated_todo_store(tmp_path):
    """构造一个 tmp-backed TodoStore，设为进程级 singleton（使测试期间
    get_todo_store() 返回它），yield 后重置。供驱动 todo 工具或 agent loop
    的 todo 路径的测试使用。"""
    from twinkle.agentserver.todo import _set_todo_store
    from twinkle.agentserver.todo.store import TodoStore
    s = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(s)
    yield s
    _set_todo_store(None)
