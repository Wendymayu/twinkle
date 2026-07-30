"""cron expression validation + next-run computation.

Lazily imports croniter so the rest of the system runs fine when cron is
unused. Supports 5-field (recurring) and 7-field (one-shot with second+year).
IANA timezone via zoneinfo. CroniterBadDateError (no next date, e.g. expired
one-shot) is detected by class name / message — see _is_croniter_no_next_date.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _cron_field_count(expr: str) -> int:
    return len(expr.split())


def validate_cron_expression(expr: str, timezone: str = "UTC") -> None:
    """Raise ValueError if expr is not a valid 5/7-field cron or timezone bad."""
    from croniter import croniter
    n = _cron_field_count(expr)
    if n not in (5, 7):
        raise ValueError(f"cron expr must be 5 or 7 fields, got {n}: {expr!r}")
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expr: {expr!r}")
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ValueError(f"bad timezone {timezone!r}: {exc}") from exc


def _cron_next_push_dt(expr: str, base_dt: datetime) -> datetime:
    """Next push datetime at/after base_dt (timezone-aware)."""
    from croniter import croniter
    nxt = croniter(expr, base_dt).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=base_dt.tzinfo)
    return nxt


def _is_croniter_no_next_date(exc: BaseException) -> bool:
    """True if exc means 'no future date' (expired one-shot)."""
    name = exc.__class__.__name__
    return name == "CroniterBadDateError" or "failed to find next date" in str(exc)
