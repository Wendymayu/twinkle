"""CronSchedulerService — the gateway clock + two-phase wake→push engine.

Min-heap of _Event(wake|push|push_update). wake_dt = push_dt - wake_offset;
wake runs the agent and stores result_text in CronRunState; push delivers it
(push_update补发 if agent still running). Driven by asyncio (wait_for on a
reload_event + 5s mtime-poll). AgentServer is channel-agnostic.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.cron import cron_expr
from twinkle.gateway.cron.models import CronJob, CronRunState, _Event
from twinkle.gateway.cron.store import CronJobStore
from twinkle.schema.message import EventType, Message

log = logging.getLogger("twinkle.gateway.cron")

_STORE_POLL_INTERVAL = 5.0
_DEFAULT_WAKE_OFFSET = 60


class CronSchedulerService:
    def __init__(self, store: CronJobStore, agent_client, message_handler,
                 now_fn=time.time) -> None:
        self._store = store
        self._agent_client = agent_client
        self._message_handler = message_handler
        self._now = now_fn
        # 堆: list[(at_ts, seq, _Event)]
        self._events: list[tuple[float, int, _Event]] = []
        self._jobs: dict[str, CronJob] = {}
        self._runs: dict[str, CronRunState] = {}        # run_id -> state (内存)
        self._run_tasks: dict[str, asyncio.Task] = {}   # run_id -> _run_agent task
        self._seq = 0
        self._reload_event = asyncio.Event()
        self._last_mtime: float | None = None
        self._task: asyncio.Task | None = None

    # --- event scheduling ---
    def _schedule_event(self, at_ts: float, kind: str, job_id: str, run_id: str) -> None:
        self._seq += 1
        ev = _Event(at_ts=at_ts, seq=self._seq, kind=kind, job_id=job_id, run_id=run_id)
        heapq.heappush(self._events, (at_ts, self._seq, ev))

    def _compute_next_run(self, job: CronJob, now_ts: float):
        tz = ZoneInfo(job.timezone)
        base = datetime.fromtimestamp(now_ts, tz=tz)
        push_dt = cron_expr._cron_next_push_dt(job.cron_expr, base)
        offset = max(0, int(job.wake_offset_seconds or _DEFAULT_WAKE_OFFSET))
        wake_dt = push_dt - timedelta(seconds=offset)
        run_id = f"{job.id}:{int(push_dt.timestamp())}"
        return push_dt, wake_dt, run_id

    # --- store change detection (mtime) ---
    def _check_store_changed(self) -> bool:
        try:
            mtime = self._store._path.stat().st_mtime  # noqa: SLF001
        except Exception:
            return False
        if self._last_mtime is None:
            self._last_mtime = mtime  # 首次发现文件 → 算变（对齐 reload）
            return True
        changed = mtime != self._last_mtime
        self._last_mtime = mtime
        return changed

    # --- reload: rebuild heap, preserve push_update ---
    async def reload(self) -> None:
        jobs = await self._store.list_jobs()
        self._jobs = {j.id: j for j in jobs if j.enabled and not j.expired}
        # 保留跨 reload 的 push_update（state 内存丢失但事件保留以尽力补发）
        pending_push_updates = [
            (at_ts, seq, ev) for at_ts, seq, ev in self._events
            if ev.kind == "push_update"
        ]
        self._events.clear()
        self._seq = 0
        for item in pending_push_updates:
            heapq.heappush(self._events, item)
        now_ts = self._now()
        for job in self._jobs.values():
            try:
                push_dt, wake_dt, run_id = self._compute_next_run(job, now_ts)
            except Exception as exc:
                if cron_expr._is_croniter_no_next_date(exc):
                    await self._store.update_job(job.id, {"enabled": False, "expired": True})
                continue
            self._schedule_event(wake_dt.timestamp(), "wake", job.id, run_id)
            self._schedule_event(push_dt.timestamp(), "push", job.id, run_id)
        try:
            self._last_mtime = self._store._path.stat().st_mtime  # noqa: SLF001
        except Exception:
            self._last_mtime = None
        self._reload_event.set()
