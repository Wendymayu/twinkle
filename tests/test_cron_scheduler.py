"""CronSchedulerService tests (skeleton + wake/push/loop)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from twinkle.gateway.cron.models import CronJob, CronRunState, _Event
from twinkle.gateway.cron.store import CronJobStore


def run(coro):
    return asyncio.run(coro)


class FakeAgentClient:
    """Records envelopes; returns scripted E2AResponse stream per request_id."""
    def __init__(self):
        self.sent = []          # 所有发出的 envelope
        self._scripts = {}      # request_id -> list[E2AResponse]
        self._denied = set()    # 收到 deny 的 approval_id

    def script(self, request_id, responses):
        self._scripts[request_id] = list(responses)

    async def _send(self, envelope):
        self.sent.append(envelope)
        if envelope.method == "approval.respond":
            self._denied.add(envelope.params.get("approval_id"))

    async def send_request_stream(self, envelope):
        from twinkle.e2a.models import E2AResponse
        rid = envelope.request_id
        for r in self._scripts.get(rid, []):
            yield r


class FakeMessageHandler:
    """Captures enqueue_outbound messages."""
    def __init__(self):
        self.outbound = []

    async def enqueue_outbound(self, msg):
        self.outbound.append(msg)


def _make_scheduler(tmp_path, agent_client=None, message_handler=None, now_fn=None):
    from twinkle.gateway.cron.scheduler import CronSchedulerService
    store = CronJobStore(tmp_path / "c.json")
    return CronSchedulerService(
        store=store,
        agent_client=agent_client or FakeAgentClient(),
        message_handler=message_handler or FakeMessageHandler(),
        now_fn=now_fn or time.time,
    )


def test_compute_next_run_wake_before_push(tmp_path):
    s = _make_scheduler(tmp_path, now_fn=lambda: 1000.0)
    job = CronJob(id="j1", name="t", cron_expr="*/5 * * * *", timezone="UTC")
    push_dt, wake_dt, run_id = s._compute_next_run(job, 1000.0)
    assert wake_dt.timestamp() == push_dt.timestamp() - 60  # wake_offset 60
    assert run_id == f"j1:{int(push_dt.timestamp())}"


def test_reload_rebuilds_heap_from_jobs(tmp_path):
    s = _make_scheduler(tmp_path)
    run(s._store.create_job({"name": "a", "cron_expr": "*/5 * * * *", "timezone": "UTC"}))
    run(s.reload())
    assert len(s._events) == 2  # 一个 wake + 一个 push


def test_reload_preserves_push_update_events(tmp_path):
    s = _make_scheduler(tmp_path)
    # 手动塞一个 push_update 事件
    s._schedule_event(999.0, "push_update", "j1", "j1:1000")
    run(s._store.create_job({"name": "a", "cron_expr": "*/5 * * * *", "timezone": "UTC"}))
    run(s.reload())
    kinds = [e[2].kind for e in s._events]
    assert "push_update" in kinds  # push_update 跨 reload 保留


def test_mtime_change_triggers_reload(tmp_path):
    s = _make_scheduler(tmp_path)
    run(s.reload())
    old_mtime = s._last_mtime
    # 外部改文件
    run(s._store.create_job({"name": "b", "cron_expr": "*/5 * * * *", "timezone": "UTC"}))
    changed = s._check_store_changed()
    assert changed is True
