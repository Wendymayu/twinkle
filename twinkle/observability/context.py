"""Request-scoped context for stamping ids onto child spans.

set_request_context(...) returns a token whose reset() goes in finally;
_llm_call_counter is reset by the agent wrap and incremented by the llm
wrap so the agent wrap can stamp twinkle.agent.iterations at span end.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class RequestContext:
    request_id: str | None = None
    session_id: str | None = None
    agent_name: str | None = None


_request_context: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "twinkle_obs_request_context", default=None
)
_llm_call_counter: contextvars.ContextVar[int] = contextvars.ContextVar(
    "twinkle_obs_llm_counter", default=0
)


class _Token:
    def __init__(self, var: contextvars.ContextVar, token: contextvars.Token) -> None:
        self._var = var
        self._token = token

    def reset(self) -> None:
        self._var.reset(self._token)


def set_request_context(**kwargs) -> _Token:
    return _Token(_request_context, _request_context.set(RequestContext(**kwargs)))


def current_request_context() -> RequestContext | None:
    return _request_context.get()


def reset_llm_counter() -> _Token:
    return _Token(_llm_call_counter, _llm_call_counter.set(0))


def increment_llm_counter() -> None:
    _llm_call_counter.set(_llm_call_counter.get() + 1)


def current_llm_counter() -> int:
    return _llm_call_counter.get()
