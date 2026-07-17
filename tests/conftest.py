"""Shared test fixtures — avoids depending on pytest-asyncio just for free ports."""
from __future__ import annotations

import socket

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
