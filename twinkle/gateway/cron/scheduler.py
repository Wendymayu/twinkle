"""CronSchedulerService — the gateway clock + two-phase wake→push engine.

Min-heap of _Event(wake|push|push_update). wake_dt = push_dt - wake_offset;
wake runs the agent and stores result_text in CronRunState; push delivers it
(push_update补发 if agent still running). Driven by asyncio (wait_for on a
reload_event + 5s mtime-poll). AgentServer is channel-agnostic.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from twinkle.e2a.models import E2AEnvelope
from twinkle.gateway.cron import cron_expr
from twinkle.gateway.cron.models import CronJob, CronRunState, _Event
from twinkle.gateway.cron.store import (
    CronJobStore,
    default_cron_jobs_path,
    default_sidecar_path,
)
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
        self._sidecar_path = default_sidecar_path()

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
        # 先捕获 mtime：reload 期间的并发写会在下次 _check_store_changed 被发现
        # （若放在末尾，reload 内的写会把并发写的 mtime 吸收掉 → 漏检）
        try:
            self._last_mtime = self._store._path.stat().st_mtime  # noqa: SLF001
        except Exception:
            self._last_mtime = None
        jobs = await self._store.list_jobs()
        self._jobs = {j.id: j for j in jobs if j.enabled and not j.expired}
        # 保留跨 reload 的 push_update（state 内存丢失但事件保留以尽力补发）
        pending_push_updates = [
            (at_ts, seq, ev) for at_ts, seq, ev in self._events
            if ev.kind == "push_update"
        ]
        self._events.clear()
        # _seq 推到保留事件的最大 seq 之上，新事件 seq 必然更大 →
        # (at_ts, seq) 不会撞车 → _Event 永不被 heapq 比较（避免 TypeError）
        self._seq = max((seq for _, seq, _ in pending_push_updates), default=0)
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
        self._reload_event.set()

    # --- run state helpers ---
    def _get_or_create_state(self, ev: _Event) -> CronRunState | None:
        state = self._runs.get(ev.run_id)
        if state is not None:
            return state
        # 用 job 自身的 wake_offset 反推（查不到 job 才回退默认值）
        job = self._jobs.get(ev.job_id)
        offset = (int(job.wake_offset_seconds)
                  if job is not None else _DEFAULT_WAKE_OFFSET)
        # 反推 wake/push iso（从 run_id 末尾 push_ts）
        try:
            push_ts = int(ev.run_id.rsplit(":", 1)[1])
            push_dt = datetime.fromtimestamp(push_ts, tz=ZoneInfo("UTC"))
            wake_dt = push_dt - timedelta(seconds=offset)
            push_iso, wake_iso = push_dt.isoformat(), wake_dt.isoformat()
        except Exception:
            push_iso = wake_iso = ""
        state = CronRunState(run_id=ev.run_id, job_id=ev.job_id,
                             wake_at_iso=wake_iso, push_at_iso=push_iso)
        self._runs[ev.run_id] = state
        return state

    # --- wake: kick off agent run ---
    async def _on_wake(self, ev: _Event) -> None:
        job = self._jobs.get(ev.job_id)
        if job is None or not job.enabled:
            return
        state = self._get_or_create_state(ev)
        existing = self._run_tasks.get(ev.run_id)
        if existing is not None and not existing.done():
            return  # 已在跑，不重复
        task = asyncio.create_task(self._run_agent(job, state))
        self._run_tasks[ev.run_id] = task

    # --- push: deliver result or placeholder ---
    async def _on_push(self, ev: _Event) -> None:
        job = self._jobs.get(ev.job_id)
        if job is None:
            return
        state = self._runs.get(ev.run_id)
        if state is None:
            return
        if state.pushed_final:
            return
        if state.result_text is not None:
            await self._push_to_targets(job, state, state.result_text, is_placeholder=False)
            state.pushed_final = True
        else:
            text = f"[cron] {job.name} 正在执行中，结果稍后补发"
            await self._push_to_targets(job, state, text, is_placeholder=True)
            state.placeholder_sent = True
        await self._after_push(job, state)

    # --- push_update: deliver real result after placeholder ---
    async def _on_push_update(self, ev: _Event) -> None:
        state = self._runs.get(ev.run_id)
        if state is None or state.pushed_final or not state.result_text:
            return
        job = self._jobs.get(ev.job_id)
        if job is None:
            # job 可能已删；退回 web 通道补发
            job = CronJob(id=ev.job_id, name="(deleted)", cron_expr="* * * * *",
                          timezone="UTC", targets="web")
        await self._push_to_targets(job, state, state.result_text, is_placeholder=False)
        state.pushed_final = True

    async def _after_push(self, job: CronJob, state: CronRunState) -> None:
        if job.delete_after_run:
            await self._store.delete_job(job.id)
            self._jobs.pop(job.id, None)
            return
        # 算下一次；CroniterBadDateError → expired
        try:
            push_dt, wake_dt, run_id = self._compute_next_run(job, self._now())
        except Exception as exc:
            if cron_expr._is_croniter_no_next_date(exc):
                await self._store.update_job(job.id, {"enabled": False, "expired": True})
            return
        self._schedule_event(wake_dt.timestamp(), "wake", job.id, run_id)
        self._schedule_event(push_dt.timestamp(), "push", job.id, run_id)

    # --- run agent + extract result / handle approval ---
    async def _run_agent(self, job: CronJob, state: CronRunState) -> None:
        run_id = state.run_id
        request_id = f"cron-{run_id}"
        envelope = E2AEnvelope(
            request_id=request_id, channel="__cron__",
            session_id=f"cron_{int(self._now())}_{job.id}",
            method="chat.send", params={"query": job.description},
        )
        state.status = "running"
        state.started_at = self._now()
        saw_approval = False
        denied_tools: list[str] = []
        try:
            async for resp in self._agent_client.send_request_stream(envelope):
                if resp.response_kind == "e2a.ask":
                    approval_id = resp.body.get("approval_id")
                    # 发 deny 让 agent loop 解挂（避免挂起泄漏）；drain 不 break，
                    # 否则 agent 若再起需审批的工具会注册新 approval_id 且无人 deny
                    # → await future 永挂 → 任务泄漏在 active[sid]/APPROVAL_REGISTRY
                    if approval_id:
                        await self._agent_client._send(E2AEnvelope(  # noqa: SLF001
                            request_id=f"cron-deny-{run_id}", channel="__cron__",
                            method="approval.respond",
                            params={"approval_id": approval_id, "decision": "deny"},
                        ))
                    saw_approval = True
                    denied_tools.append(resp.body.get("tool", "unknown"))
                    continue  # 继续排空流，不 break
                if resp.is_final:
                    if saw_approval:
                        # approval 被拒 = failed 语义（spec §8），覆盖 agent 的 complete
                        state.status = "failed"
                        state.error = (f"cron 任务触发了需审批的工具 "
                                       f"{','.join(denied_tools)}，已中止")
                        state.result_text = f"[cron] {state.error}"
                    elif resp.response_kind == "e2a.error":
                        state.status = "failed"
                        state.result_text = (f"[cron] 任务失败："
                                             f"{resp.body.get('error', '未知错误')}")
                        state.error = state.result_text
                    else:  # e2a.complete
                        content = (resp.body.get("result") or {}).get("content", "")
                        state.status = "succeeded"
                        state.result_text = content or "[cron] 任务完成，但未返回可展示文本"
                    break
            else:
                # 流结束但无 is_final
                if saw_approval:
                    state.status = "failed"
                    state.error = (f"cron 任务触发了需审批的工具 "
                                   f"{','.join(denied_tools)}，已中止")
                    state.result_text = f"[cron] {state.error}"
                else:
                    state.status = "failed"
                    state.error = "[cron] agent 未返回最终结果"
                    state.result_text = state.error
        except Exception as exc:
            state.status = "failed"
            state.error = f"[cron] agent 执行异常：{exc}"
            state.result_text = state.error
        finally:
            state.finished_at = self._now()
            self._run_tasks.pop(run_id, None)
            # 已发占位且未推最终 → 安排 push_update 补发
            if state.placeholder_sent and not state.pushed_final and state.result_text:
                self._schedule_event(self._now(), "push_update", job.id, run_id)
                self._reload_event.set()

    # --- push to targets (web channel via enqueue_outbound) ---
    async def _push_to_targets(self, job: CronJob, state: CronRunState,
                               text: str, is_placeholder: bool) -> None:
        msg = Message(
            id=f"cron-push-{state.run_id}-{job.targets}",
            type="event",  # 出站推送，非 inbound req
            channel_id=job.targets or "web",
            event_type=EventType.CHAT_FINAL,
            content=text,
        )
        await self._message_handler.enqueue_outbound(msg)

    # --- event dispatch (used by _loop) ---
    async def _handle_event(self, ev: _Event) -> None:
        job = self._jobs.get(ev.job_id)
        # push_update 即使 job disabled/expired 也放行
        if job is None and ev.kind != "push_update":
            return
        if job is not None and not job.enabled and ev.kind != "push_update":
            return
        if ev.kind == "wake":
            await self._on_wake(ev)
        elif ev.kind == "push":
            await self._on_push(ev)
        elif ev.kind == "push_update":
            await self._on_push_update(ev)

    # --- trigger immediate run ---
    async def trigger_run_now(self, job_id: str) -> None:
        job = await self._store.get_job(job_id)
        if job is None:
            return
        self._jobs[job.id] = job
        now_ts = self._now()
        # wake_dt = push_dt = now → 立即 wake + 立即 push
        run_id = f"{job.id}:now{int(now_ts * 1000) % 1000000}"
        self._schedule_event(now_ts, "wake", job.id, run_id)
        self._schedule_event(now_ts, "push", job.id, run_id)
        self._reload_event.set()

    # --- main loop (asyncio-driven, no thread) ---
    async def _loop(self) -> None:
        while True:
            now_ts = self._now()
            delay = 1.0
            if self._events:
                top_at = self._events[0][0]
                delay = max(0.0, top_at - now_ts)
            timeout = min(delay, _STORE_POLL_INTERVAL) if self._events else _STORE_POLL_INTERVAL
            try:
                await asyncio.wait_for(self._reload_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._reload_event.clear()
            # mtime 兜底
            if self._check_store_changed():
                await self.reload()
            # run_now sidecar 检测
            try:
                if self._sidecar_path.exists():
                    data = json.loads(self._sidecar_path.read_text(encoding="utf-8"))
                    jid = data.get("job_id")
                    if jid:
                        await self.trigger_run_now(jid)
                    self._sidecar_path.unlink(missing_ok=True)
            except Exception:
                pass
            # 处理到期事件
            now_ts = self._now()
            while self._events and self._events[0][0] <= now_ts:
                _, _, ev = heapq.heappop(self._events)
                try:
                    await self._handle_event(ev)
                except Exception:
                    log.exception("cron event handling failed: %s", ev.kind)

    async def start(self) -> None:
        await self.reload()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._run_tasks.values()):
            t.cancel()
