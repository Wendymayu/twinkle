# tests/test_skill_e2e.py
"""E2E: chat.send -> list_skill -> read_skill -> complete, through the real
ws_handler + gateway MessageHandler + AgentClient, on a free port.
Uses a scripted LLM (no real API calls) + a tmp-backed skill (demo)."""
import asyncio
import importlib
from pathlib import Path

from websockets.asyncio.server import serve

from twinkle.agentserver.server import ws_handler, build_agent_loop
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.skills import _set_skill_manager, SkillManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.agent_client import AgentClient
from twinkle.schema.message import Message


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        evs = self._scripts[self.calls]
        self.calls += 1
        for ev in evs:
            yield ev


def _make_skill(dir_: Path, name: str, desc: str) -> None:
    dir_.mkdir(parents=True)
    (dir_ / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n## flow\nstep\n", encoding="utf-8"
    )


def test_skill_flow_through_gateway_and_agentserver(free_port, tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import twinkle.config as cfg
    importlib.reload(cfg)

    # 造一个真 skill(demo)+ 注入单例
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir / "demo", "demo", "a demo skill")
    _set_skill_manager(SkillManager(str(skills_dir)))
    try:
        scripted = _ScriptedLLM([
            # 第 1 轮:模型调 list_skill(看见工具 schema)
            [Finish("tool_calls", {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "list_skill", "arguments": "{}"}}]})],
            # 第 2 轮:模型调 read_skill("demo")
            [Finish("tool_calls", {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c2", "type": "function",
                                "function": {"name": "read_skill",
                                             "arguments": '{"skill_name":"demo"}'}}]})],
            # 第 3 轮:最终回答
            [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
        ])
        from twinkle.agentserver.sessions import session_store

        store = session_store()
        # hooks=[]:本 E2E 不验 SkillHook 注入(单测 test_skill_hook 覆盖),
        # 只验工具链(list_skill/read_skill 经 tool_manager 执行)+ tool_result 回灌
        loop = build_agent_loop(store, hooks=[], llm=scripted)

        async def scenario():
            handler = ws_handler(loop, store)
            srv = await serve(handler, "127.0.0.1", free_port)
            try:
                ac = AgentClient(f"ws://127.0.0.1:{free_port}")
                await ac.connect()
                mh = MessageHandler(ac)
                # 1. inbound chat.send (R)
                msg = Message(id="R", type="req", channel_id="web", session_id="s1",
                              method="chat.send", params={"query": "use demo skill"})
                await mh.handle_message(msg)
                # 2. drain 出站帧直到 chat.final(中间会有 tool_result 不出帧,只 final 出)
                got_final = False
                for _ in range(20):
                    out = await asyncio.wait_for(mh.dequeue_outbound(), timeout=10)
                    if out.event_type and out.event_type.value == "chat.final":
                        got_final = True
                        break
                assert got_final, "expected chat.final after list_skill -> read_skill -> stop"
                await ac.close()
            finally:
                srv.close()
                await srv.wait_closed()

        asyncio.run(scenario())
    finally:
        _set_skill_manager(None)
