"""CronJob / CronRunState / _Event dataclass tests."""
from __future__ import annotations

import pytest

from twinkle.gateway.cron.models import CronJob, CronRunState, _Event


def test_cronjob_defaults():
    j = CronJob(id="abc", name="每日提醒", cron_expr="*/5 * * * *",
                timezone="Asia/Shanghai")
    assert j.enabled is True
    assert j.wake_offset_seconds == 60
    assert j.expired is False
    assert j.targets == "web"
    assert j.delete_after_run is False
    assert j.description == ""


def test_cronjob_to_dict_from_dict_roundtrip():
    j = CronJob(id="abc", name="日报", cron_expr="0 9 * * *",
                timezone="UTC", wake_offset_seconds=120,
                description="生成日报", targets="web",
                delete_after_run=True, created_at=1.0, updated_at=2.0)
    d = j.to_dict()
    assert d["id"] == "abc" and d["wake_offset_seconds"] == 120
    j2 = CronJob.from_dict(d)
    assert j2.name == "日报" and j2.delete_after_run is True


def test_cronjob_from_dict_missing_required_raises():
    with pytest.raises(ValueError):
        CronJob.from_dict({"id": "abc"})  # 缺 name/cron_expr/timezone


def test_cronrunstate_defaults():
    s = CronRunState(run_id="abc:1000", job_id="abc",
                     wake_at_iso="2026-01-01T00:00:00+00:00",
                     push_at_iso="2026-01-01T00:01:00+00:00")
    assert s.status == "pending"
    assert s.placeholder_sent is False
    assert s.pushed_final is False
    assert s.result_text is None


def test_event_frozen():
    ev = _Event(at_ts=100.0, seq=1, kind="wake", job_id="abc", run_id="abc:1000")
    with pytest.raises(Exception):
        ev.kind = "push"  # frozen dataclass 不可变
