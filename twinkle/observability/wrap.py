"""patch_method — idempotent, fail-soft monkey-patch helper.

Borrowed from jiuwenswarm-instrumentor wrap.py. Marks wrappers with
_twinkle_wrapped so repeated patches are no-ops; any failure is logged
and skipped, never raised into the host.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("twinkle.observability.wrap")

_WRAPPED_MARKER = "_twinkle_wrapped"


def patch_method(cls: type, name: str, factory: Callable[[Any], Any]) -> bool:
    """Patch ``cls.<name>`` with ``factory(original)``.

    Idempotent (already-wrapped -> no-op) and fail-soft (any error -> log +
    skip, never raise into the host). Returns True if patched, False if
    skipped.
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
