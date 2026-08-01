"""cron_expr: validation + next-run + bad-date detection."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from twinkle.gateway.cron import cron_expr


def test_validate_5_field_ok():
    cron_expr.validate_cron_expression("*/5 * * * *")  # 不抛


def test_validate_7_field_ok():
    cron_expr.validate_cron_expression("0 9 1 1 * 0 2026")  # 不抛


def test_validate_bad_field_count_raises():
    with pytest.raises(ValueError):
        cron_expr.validate_cron_expression("* * *")  # 3 field


def test_validate_invalid_expr_raises():
    with pytest.raises(ValueError):
        cron_expr.validate_cron_expression("99 99 * * *")  # croniter 不认


def test_validate_bad_timezone_raises():
    with pytest.raises(ValueError):
        cron_expr.validate_cron_expression("*/5 * * * *", timezone="Mars/Olympus")


def test_next_push_dt_uses_timezone():
    # base 00:59 UTC，expr "*/5 * * * *" → 下一次是 01:00
    base = datetime(2026, 1, 1, 0, 59, tzinfo=timezone.utc)
    nxt = cron_expr._cron_next_push_dt("*/5 * * * *", base)
    assert nxt.minute == 0 and nxt.hour == 1


def test_is_no_next_date_detects_croniter_bad_date():
    class FakeExc(Exception):
        pass

    FakeExc.__name__ = "CroniterBadDateError"
    assert cron_expr._is_croniter_no_next_date(FakeExc("failed to find next date"))
