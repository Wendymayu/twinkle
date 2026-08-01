"""集成测试: AgentServer + gateway 端到端 cron wake→push。

用 scripted LLM（不打真实 API）+ 单进程装配 AgentLoop + CronSchedulerService
（fake agent_client 直连 loop）+ CapturingHandler，验证 wake→_run_agent→push→
enqueue_outbound 全链路，web channel 收到 chat.final。这是 roadmap §Phase 6
验收。
"""
from __future__ import annotations

import asyncio

from twinkle.agentserver.llm_client import Finish, TextDelta


class ScriptedLLM:
    """总是返回固定 Finish（content='集成结果OK'），无 tool call。"""

    async def stream(self, messages, tools):
        yield TextDelta("集成结果OK")
        yield Finish(
            finish_reason="stop",
            assistant_message={"role": "assistant", "content": "集成结果OK"},
        )


def test_e2e_cron_wake_push(tmp_path):
    """端到端: 注册 job → run_now → web 收到结果。

    为避免起两进程的复杂度，本测试在单进程内装配 AgentLoop(scripted LLM)
    + CronSchedulerService(fake agent_client 直连 loop) + CapturingHandler，
    验证 wake→_run_agent→push→enqueue_outbound 全链路。
    """
    from twinkle.agentserver.agent_loop import AgentLoop
    from twinkle.agentserver.sessions import SessionStore
    from twinkle.agentserver.tools import tool_manager
    from twinkle.gateway.cron.models import CronJob, _Event
    from twinkle.gateway.cron.scheduler import CronSchedulerService
    from twinkle.gateway.cron.store import CronJobStore

    class LoopAgentClient:
        """agent_client 直连 AgentLoop.run_stream（单进程捷径）。"""

        def __init__(self, loop):
            self._loop = loop
            self.sent = []

        async def _send(self, envelope):
            self.sent.append(envelope)

        async def send_request_stream(self, envelope):
            async for resp in self._loop.run_stream(envelope):
                yield resp

    class CapturingHandler:
        def __init__(self):
            self.outbound = []

        async def enqueue_outbound(self, msg):
            self.outbound.append(msg)

    async def body():
        store_path = tmp_path / "cron_jobs.json"
        store = CronJobStore(store_path)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        loop = AgentLoop(ScriptedLLM(), SessionStore(str(sessions)), tool_manager())
        ac = LoopAgentClient(loop)
        mh = CapturingHandler()
        clock = [1000.0]
        sched = CronSchedulerService(store, ac, mh, now_fn=lambda: clock[0])
        job = CronJob(
            id="ie",
            name="集成",
            cron_expr="0 9 * * *",
            timezone="UTC",
            description="做集成测试",
            wake_offset_seconds=0,
        )
        # ⚠️ Task 6 教训: CronJobStore.create_job 用 _new_id() 覆盖传入 id！
        # 所以用 created.id（真实 uuid），不要用固定 "ie" 调 trigger_run_now。
        created = await store.create_job(job.to_dict())
        await sched.reload()
        await sched.trigger_run_now(created.id)
        # 找 wake run_id（trigger_run_now 用 created.id 安排事件）
        wake_ev = next(
            e[2]
            for e in sched._events
            if e[2].kind == "wake" and e[2].job_id == created.id
        )
        state = sched._get_or_create_state(wake_ev)
        await sched._run_agent(job, state)
        push_ev = _Event(
            at_ts=clock[0],
            seq=0,
            kind="push",
            job_id=created.id,
            run_id=wake_ev.run_id,
        )
        await sched._on_push(push_ev)
        contents = [m.content for m in mh.outbound]
        assert any("集成结果OK" in c for c in contents), f"未收到结果: {contents}"

    asyncio.run(body())
