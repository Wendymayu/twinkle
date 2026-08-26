"""patch_method —— 幂等、fail-soft 的 monkey-patch helper。

取自 jiuwenswarm-instrumentor wrap.py。用 _twinkle_wrapped 标记 wrapper，
使重复 patch 成为 no-op；任何失败均记日志并跳过，绝不向 host 抛出。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("twinkle.observability.wrap")

_WRAPPED_MARKER = "_twinkle_wrapped"


def patch_method(cls: type, name: str, factory: Callable[[Any], Any]) -> bool:
    """用 ``factory(original)`` patch ``cls.<name>``。

    幂等（已包装 -> no-op）且 fail-soft（任何错误 -> 记日志 + 跳过，
    绝不向 host 抛出）。patched 返回 True，skipped 返回 False。
    """
    try:
        original = getattr(cls, name)
    except AttributeError:
        log.warning("patch_method: %s.%s not found; skip", cls.__name__, name)
        return False
    if getattr(original, _WRAPPED_MARKER, False):
        return False
    try:
        wrapped = factory(original)
        setattr(wrapped, _WRAPPED_MARKER, True)
        setattr(wrapped, "__wrapped__", original)
        setattr(cls, name, wrapped)
        return True
    except Exception:
        log.exception("patch_method: failed to wrap %s.%s; skip", cls.__name__, name)
        return False
