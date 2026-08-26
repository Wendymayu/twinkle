"""cron 表达式校验 + 下次运行时间计算。

懒加载 croniter，使得 cron 未使用时系统的其余部分照常运行。支持 5 字段（循环
触发）和 7 字段（含秒+年的一次性触发）。通过 zoneinfo 使用 IANA 时区。
CroniterBadDateError（无下一个日期，例如已过期的一次性触发）通过类名 / 消息
识别 —— 见 _is_croniter_no_next_date。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _cron_field_count(expr: str) -> int:
    return len(expr.split())


def validate_cron_expression(expr: str, timezone: str = "UTC") -> None:
    """若 expr 不是合法的 5/7 字段 cron 或时区不合法，则 raise ValueError。"""
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
    """base_dt 之后的下次 push 时刻（带时区）。"""
    from croniter import croniter
    nxt = croniter(expr, base_dt).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=base_dt.tzinfo)
    return nxt


def _is_croniter_no_next_date(exc: BaseException) -> bool:
    """若 exc 表示'无未来日期'（已过期的一次性触发），返回 True。"""
    name = exc.__class__.__name__
    return name == "CroniterBadDateError" or "failed to find next date" in str(exc)
