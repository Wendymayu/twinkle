"""Read token counts from an LLM usage object.

The real openai SDK exposes ``Finish.usage`` as a ``CompletionUsage`` pydantic
object (attributes, no ``.get``); fakes/tests use plain dicts. Helpers here
treat both uniformly so instrumentors never assume dict shape.
"""
from __future__ import annotations

from typing import Any


def read_usage_token(usage: Any, *keys: str) -> Any:
    """First non-None value among ``keys`` on ``usage``.

    Works on dicts (``.get``) and on attribute objects (``getattr``); missing
    keys resolve to ``None``. Returns ``None`` when ``usage`` is falsy.
    """
    if not usage:
        return None
    if isinstance(usage, dict):
        for k in keys:
            v = usage.get(k)
            if v is not None:
                return v
        return None
    for k in keys:
        v = getattr(usage, k, None)
        if v is not None:
            return v
    return None
