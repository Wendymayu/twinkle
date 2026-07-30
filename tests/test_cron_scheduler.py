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


def test_reload_seq_no_collision_with_push_update(tmp_path):
    """Fix 9: reload 后新事件 seq 严格大于保留 push_update 的 seq，
    避免共用 (at_ts, seq) 触发 _Event 比较 TypeError。"""
    import heapq
    s = _make_scheduler(tmp_path, now_fn=lambda: 1000.0)
    # 手动塞一个 push_update（高 seq），模拟跨 reload 保留事件
    s._seq = 50
    s._schedule_event(999.0, "push_update", "j1", "j1:1000")  # seq=51
    run(s._store.create_job({"name": "a", "cron_expr": "*/5 * * * *", "timezone": "UTC"}))
    run(s.reload())
    # 新事件 seq 必须 > 51（Fix 9 把 _seq 推到 max(preserved) 之上再递增）
    new_seqs = [seq for _, seq, ev in s._events if ev.kind != "push_update"]
    assert new_seqs, "应排了 wake/push 事件"
    assert all(seq > 51 for seq in new_seqs), f"seq 撞车风险: {new_seqs}"
    # 全弹出不应抛 TypeError，且按 (at_ts, seq) 升序
    heap = list(s._events)
    heapq.heapify(heap)
    popped = [heapq.heappop(heap) for _ in range(len(heap))]
    keys = [(p[0], p[1]) for p in popped]
    assert keys == sorted(keys)


def test_mtime_change_triggers_reload(tmp_path):
    s = _make_scheduler(tmp_path)
    run(s.reload())
    old_mtime = s._last_mtime
    # 外部改文件
    run(s._store.create_job({"name": "b", "cron_expr": "*/5 * * * *", "timezone": "UTC"}))
    changed = s._check_store_changed()
    assert changed is True


# ---- Task 5: wake / push / push_update / approval ----

def _e2a_complete(rid, content):
    from twinkle.e2a.models import E2AResponse
    return E2AResponse(request_id=rid, is_final=True, status="succeeded",
                      response_kind="e2a.complete",
                      body={"result": {"content": content}})

def _e2a_ask(rid, approval_id, tool="dangerous_tool"):
    from twinkle.e2a.models import E2AResponse
    return E2AResponse(request_id=rid, is_final=False, status="in_progress",
                      response_kind="e2a.ask",
                      body={"approval_id": approval_id, "tool": tool,
                            "args": {}, "tool_call_id": "tc1", "reason": "需要审批"})


def test_on_push_pushes_result_when_agent_done(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j1", name="日报", cron_expr="*/5 * * * *", timezone="UTC",
                  description="生成日报")
    run(s._store.create_job(job.to_dict() | {"id": "j1"}))  # 用固定 id
    # 先 manually 建一个已完成 run state（模拟 wake 已跑完）
    state = CronRunState(run_id="j1:2000", job_id="j1",
                         wake_at_iso="...", push_at_iso="...",
                         status="succeeded", result_text="日报内容OK")
    s._runs["j1:2000"] = state
    s._jobs["j1"] = job
    ev = _Event(at_ts=2000.0, seq=1, kind="push", job_id="j1", run_id="j1:2000")
    run(s._on_push(ev))
    assert state.pushed_final is True
    assert mh.outbound and mh.outbound[0].content == "日报内容OK"
    assert mh.outbound[0].event_type.value == "chat.final"


def test_on_push_sends_placeholder_when_agent_not_done(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j2", name="慢任务", cron_expr="*/5 * * * *", timezone="UTC")
    s._jobs["j2"] = job
    state = CronRunState(run_id="j2:2000", job_id="j2",
                         wake_at_iso="...", push_at_iso="...")  # result_text None
    s._runs["j2:2000"] = state
    ev = _Event(at_ts=2000.0, seq=1, kind="push", job_id="j2", run_id="j2:2000")
    run(s._on_push(ev))
    assert state.placeholder_sent is True
    assert "正在执行中" in mh.outbound[0].content


def test_run_agent_schedules_push_update_after_placeholder(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    # wake 在 push 之前：先 push 发占位，再 wake 跑完安排 push_update
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j3", name="x", cron_expr="*/5 * * * *", timezone="UTC",
                  description="做x")
    s._jobs["j3"] = job
    state = CronRunState(run_id="j3:2000", job_id="j3",
                         wake_at_iso="...", push_at_iso="...", placeholder_sent=True)
    s._runs["j3:2000"] = state
    ac.script("cron-j3:2000", [_e2a_complete("cron-j3:2000", "结果X")])
    run(s._run_agent(job, state))
    assert state.status == "succeeded"
    assert state.result_text == "结果X"
    assert state.pushed_final is False  # push_update 还没执行
    # 安排了 push_update 事件
    kinds = [e[2].kind for e in s._events]
    assert "push_update" in kinds


def test_on_push_update_delivers_real_result(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j4", name="x", cron_expr="*/5 * * * *", timezone="UTC")
    s._jobs["j4"] = job
    state = CronRunState(run_id="j4:2000", job_id="j4",
                         wake_at_iso="...", push_at_iso="...",
                         placeholder_sent=True, result_text="真实结果")
    s._runs["j4:2000"] = state
    ev = _Event(at_ts=2000.0, seq=1, kind="push_update", job_id="j4", run_id="j4:2000")
    run(s._on_push_update(ev))
    assert state.pushed_final is True
    assert mh.outbound[-1].content == "真实结果"


def test_run_agent_approval_sends_deny_and_fails(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j5", name="x", cron_expr="*/5 * * * *", timezone="UTC",
                  description="需要审批的工具")
    state = CronRunState(run_id="j5:2000", job_id="j5",
                         wake_at_iso="...", push_at_iso="...")
    s._runs["j5:2000"] = state
    ac.script("cron-j5:2000", [_e2a_ask("cron-j5:2000", "apv1", "dangerous_tool")])
    run(s._run_agent(job, state))
    assert state.status == "failed"
    assert "需审批" in (state.error or "")
    # 发了 deny 让 agent loop 解挂
    assert "apv1" in ac._denied


def test_run_agent_drains_multiple_asks_then_final_fails(tmp_path):
    """Fix 1: 多个 e2a.ask 全部 deny 后继续排空流到 is_final；
    即使 agent 返回 complete，saw_approval=True → 覆盖为 failed（spec §8）。"""
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j9", name="x", cron_expr="*/5 * * * *", timezone="UTC",
                  description="多次审批")
    state = CronRunState(run_id="j9:2000", job_id="j9",
                         wake_at_iso="...", push_at_iso="...")
    s._runs["j9:2000"] = state
    rid = "cron-j9:2000"
    ac.script(rid, [
        _e2a_ask(rid, "apv1", "tool1"),
        _e2a_ask(rid, "apv2", "tool2"),
        _e2a_complete(rid, "不该被采用的结果"),
    ])
    run(s._run_agent(job, state))
    # 两个 approval 都被 deny（drain 不 break）
    assert "apv1" in ac._denied and "apv2" in ac._denied
    # saw_approval=True → failed，complete 内容被覆盖
    assert state.status == "failed"
    assert "tool1" in (state.error or "") and "tool2" in (state.error or "")
    assert "不该被采用的结果" not in (state.result_text or "")


def test_delete_after_run_deletes_after_push(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j6", name="once", cron_expr="0 9 1 1 * 0 2026",
                  timezone="UTC", delete_after_run=True)
    s._jobs["j6"] = job
    state = CronRunState(run_id="j6:2000", job_id="j6",
                         wake_at_iso="...", push_at_iso="...",
                         result_text="done")
    s._runs["j6:2000"] = state
    ev = _Event(at_ts=2000.0, seq=1, kind="push", job_id="j6", run_id="j6:2000")
    run(s._on_push(ev))
    assert run(s._store.get_job("j6")) is None  # 已删


# ---- Task 6: loop + trigger_run_now + start/stop ----

def test_trigger_run_now_schedules_immediate_wake_push(tmp_path):
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: 1000.0)
    job = CronJob(id="j7", name="now", cron_expr="0 9 * * *", timezone="UTC",
                  description="立即做")
    created = run(s._store.create_job(job.to_dict()))
    run(s.reload())
    run(s.trigger_run_now(created.id))
    # 安排了 wake + push，at_ts ≈ now(1000)
    wakes = [e for e in s._events if e[2].kind == "wake"]
    pushes = [e for e in s._events if e[2].kind == "push"]
    assert len(wakes) >= 1 and len(pushes) >= 1


def test_loop_drives_events_to_completion(tmp_path):
    """用 trigger_run_now + 短超时跑真实 _loop，验证 wake→push 端到端。"""
    ac = FakeAgentClient(); mh = FakeMessageHandler()
    clock = [1000.0]
    s = _make_scheduler(tmp_path, ac, mh, now_fn=lambda: clock[0])
    job = CronJob(id="j8", name="e2e", cron_expr="0 9 * * *", timezone="UTC",
                  description="端到端", wake_offset_seconds=0)
    created = run(s._store.create_job(job.to_dict()))
    jid = created.id
    run(s.reload())
    run(s.trigger_run_now(jid))
    wake_ev = next(e[2] for e in s._events if e[2].kind == "wake" and e[2].job_id == jid)
    ac.script(f"cron-{wake_ev.run_id}", [_e2a_complete(f"cron-{wake_ev.run_id}", "E2E结果")])

    async def drive():
        s._task = asyncio.create_task(s._loop())
        for _ in range(50):
            await asyncio.sleep(0.01)
            clock[0] += 0.05  # 推进时间让 push 到点
        s._task.cancel()
        try:
            await s._task
        except asyncio.CancelledError:
            pass
    run(drive())
    contents = [m.content for m in mh.outbound]
    assert "E2E结果" in contents
