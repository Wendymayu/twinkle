"""End-to-end Phase 1 integration: the full browser -> gateway ->
agentserver -> gateway -> browser round trip, driven by a REAL AgentLoop
with a FAKE LLMClient (deterministic, no API key).

Exercises: streaming chunks, tool round-trip, and cross-turn memory —
the roadmap Phase 1 / M2 acceptance, headlessly.
"""
import asyncio
import json
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.server import ws_handler
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.decorator import tool
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel


class _ScriptedLLM:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    async def stream(self, messages, tools):
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


class _FakeSkillNetClient:
    """Network-free stand-in for SkillNetClient: a canned catalog + a canned
    downloaded skill dir. Lets the gateway-seam e2e run without hitting GitHub.
    Real-GitHub coverage is the throwaway ``_e2e_skillnet.py``."""
    def __init__(self, catalog):
        self._catalog = catalog

    async def search_remote_skills(self, q, force_refresh=False):
        # 模拟服务端关键词匹配
        ql = (q or "").lower()
        return [s for s in self._catalog if not ql or ql in s.name.lower() or ql in s.description.lower()]

    async def download_skill(self, url):
        import tempfile
        temp_root = Path(tempfile.mkdtemp(prefix="twinkle_e2e_"))
        skill_dir = temp_root / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: foo\ndescription: a foo skill\n---\nbody\n", encoding="utf-8")
        return "foo", skill_dir, temp_root


def _reg_with_echo():
    from twinkle.agentserver.tools.manager import ToolManager

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"TOOL:{text}"

    m = ToolManager()
    m.register(echo)
    return m


async def _collect_streamed(browser) -> tuple[str, bool]:
    """Collect chat.delta into chat.final. Returns (assembled, saw_final)."""
    assembled = ""
    saw_final = False
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(browser.recv(), timeout=5)
        frame = json.loads(raw)
        if frame["type"] != "event":
            continue
        if frame["event"] == "chat.delta":
            assembled += frame["payload"]["content"]
        elif frame["event"] == "chat.final":
            if frame["payload"].get("content"):
                assembled = frame["payload"]["content"]
            saw_final = True
            break
    return assembled, saw_final


def test_end_to_end_tool_round_trip(tmp_path, port_factory) -> None:
    agentserver_port = port_factory()
    gateway_port = port_factory()
    scripts = [
        # turn 1: model calls echo tool, then answers
        [Finish("tool_calls", {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "ping"}'}}]})],
        [TextDelta("answer:"), TextDelta("TOOL:ping"),
         Finish("stop", {"role": "assistant", "content": "answer:TOOL:ping", "tool_calls": None})],
    ]
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM(scripts), store, _reg_with_echo())

    async def run() -> None:
        server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
        try:
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()

            message_handler = MessageHandler(agent_client)
            channel_manager = ChannelManager(message_handler)
            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()
            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    await browser.recv()  # connection.ack
                    await browser.send(json.dumps({
                        "type": "req", "id": "r1", "method": "chat.send",
                        "params": {"query": "call echo", "session_id": "s1"},
                    }))
                    ack = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
                    assert ack["type"] == "res" and ack["ok"] is True
                    assembled, saw_final = await _collect_streamed(browser)
                    assert saw_final
                    assert "answer:TOOL:ping" in assembled
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


async def _collect_result(browser) -> dict:
    """Read frames until a `result` event arrives; skip deltas/acks. 5s deadline."""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(browser.recv(), timeout=5)
        frame = json.loads(raw)
        if frame.get("type") != "event":
            continue
        if frame.get("event") != "result":
            continue
        return frame["payload"]
    raise AssertionError("no result event within 5s")


async def _read_ack(browser) -> dict:
    ack = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
    assert ack["type"] == "res" and ack["ok"] is True, f"bad ack: {ack}"
    return ack


def test_session_rpc_round_trip(tmp_path, port_factory) -> None:
    """Exercises the full browser -> gateway -> AgentServer `result` event
    framing for session.list / session.create / history.get RPCs. RPCs don't
    run the ReAct loop, so a trivial scripted LLM (no scripts) is fine."""
    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    async def run() -> None:
        # pre-seed a session so session.list has something to report
        await store.create_session("s-seed")
        await store.append(
            "s-seed", {"role": "user", "content": "hello"}, request_id="r0"
        )

        server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
        try:
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()

            message_handler = MessageHandler(agent_client)
            channel_manager = ChannelManager(message_handler)
            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()
            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    await browser.recv()  # connection.ack

                    # session.list
                    await browser.send(json.dumps({
                        "type": "req", "id": "r-list",
                        "method": "session.list",
                        "params": {"session_id": "s-seed"},
                    }))
                    await _read_ack(browser)
                    payload = await _collect_result(browser)
                    assert payload["type"] == "session.list"
                    sids = [s["session_id"] for s in payload["sessions"]]
                    assert "s-seed" in sids

                    # session.create
                    await browser.send(json.dumps({
                        "type": "req", "id": "r-create",
                        "method": "session.create",
                        "params": {"session_id": "s-new"},
                    }))
                    await _read_ack(browser)
                    payload = await _collect_result(browser)
                    assert payload["type"] == "session.create"
                    assert payload["session_id"] == "s-new"

                    # history.get
                    await browser.send(json.dumps({
                        "type": "req", "id": "r-history",
                        "method": "history.get",
                        "params": {"session_id": "s-seed"},
                    }))
                    await _read_ack(browser)
                    payload = await _collect_result(browser)
                    assert payload["type"] == "history.get"
                    roles = [m["role"] for m in payload["messages"]]
                    assert "user" in roles
                    assert any(
                        m.get("content") == "hello" for m in payload["messages"]
                    )
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_session_files_ws_round_trip(tmp_path, port_factory) -> None:
    """Exercises the full browser -> gateway -> AgentServer ws path for
    session.files + file.read RPCs, asserting result events carry the
    file list + content. RPCs don't run the ReAct loop, so a trivial
    scripted LLM (no scripts) is fine."""
    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s-files"))
    asyncio.run(store.append("s-files", {"role": "user", "content": "hello"},
                              request_id="r0"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    async def run() -> None:
        server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
        try:
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()
            message_handler = MessageHandler(agent_client)
            channel_manager = ChannelManager(message_handler)
            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()
            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    await browser.recv()  # connection.ack

                    # session.files
                    await browser.send(json.dumps({
                        "type": "req", "id": "rf1", "method": "session.files",
                        "params": {"session_id": "s-files"},
                    }))
                    await asyncio.wait_for(browser.recv(), timeout=5)  # ack
                    payload = await _collect_result(browser)
                    assert payload["type"] == "session.files"
                    names = {f["name"] for f in payload["files"]}
                    assert "metadata.json" in names
                    assert "history.json" in names

                    # file.read
                    await browser.send(json.dumps({
                        "type": "req", "id": "rf2", "method": "file.read",
                        "params": {"session_id": "s-files", "name": "metadata.json"},
                    }))
                    await asyncio.wait_for(browser.recv(), timeout=5)  # ack
                    payload = await _collect_result(browser)
                    assert payload["type"] == "file.read"
                    assert payload["name"] == "metadata.json"
                    meta = json.loads(payload["content"])
                    assert meta["session_id"] == "s-files"
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_skill_rpc_round_trip(tmp_path, port_factory, monkeypatch) -> None:
    """Full browser -> gateway -> AgentServer round trip for skills.search /
    skills.install / skills.list_local. Verifies the gateway forwards skill RPCs
    and the install background-task's delayed e2a.result resolves through to a
    browser `result` event + lands on disk + list_local reflects it. RPCs don't
    run the ReAct loop, so a trivial scripted LLM (no scripts) is fine.
    Network-free (FakeSkillNetClient); real-GitHub coverage is _e2e_skillnet.py."""
    from twinkle.agentserver.skills import (
        _set_skill_manager, _set_skillnet_client, SkillManager,
    )
    from twinkle.agentserver.skills.remote import RemoteSkill

    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # install path reads `twinkle.config.SKILLS_DIR` at call time; list_local reads
    # the SkillManager singleton. Point both at the same temp dir so install lands
    # and list_local reflects it.
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    _set_skill_manager(SkillManager(str(skills_dir)))
    _set_skillnet_client(_FakeSkillNetClient(catalog=[
        RemoteSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
    ]))
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
            try:
                agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
                await agent_client.connect()
                message_handler = MessageHandler(agent_client)
                channel_manager = ChannelManager(message_handler)
                web_channel = WebChannel("127.0.0.1", gateway_port)
                channel_manager.register_channel(web_channel)
                await channel_manager.start()
                web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
                try:
                    async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                        await browser.recv()  # connection.ack

                        # skills.search (background task → delayed result)
                        await browser.send(json.dumps({
                            "type": "req", "id": "r-search",
                            "method": "skills.search",
                            "params": {"q": "foo", "session_id": "s1"},
                        }))
                        await _read_ack(browser)
                        payload = await _collect_result(browser)
                        assert payload["type"] == "skills.search"
                        assert [s["name"] for s in payload["skills"]] == ["foo"]

                        # skills.install (background task → delayed result + lands on disk)
                        await browser.send(json.dumps({
                            "type": "req", "id": "r-install",
                            "method": "skills.install",
                            "params": {"url": "url_foo", "session_id": "s1"},
                        }))
                        await _read_ack(browser)
                        payload = await _collect_result(browser)
                        assert payload["ok"] is True
                        assert payload["skill_name"] == "foo"

                        # skills.list_local (inline → reflects the just-installed skill)
                        await browser.send(json.dumps({
                            "type": "req", "id": "r-list",
                            "method": "skills.list_local",
                            "params": {"session_id": "s1"},
                        }))
                        await _read_ack(browser)
                        payload = await _collect_result(browser)
                        assert payload["type"] == "skills.list_local"
                        assert [s["name"] for s in payload["skills"]] == ["foo"]

                    assert (skills_dir / "foo" / "SKILL.md").is_file()
                finally:
                    web_server.close()
                    await web_server.wait_closed()
                    await channel_manager.stop()
                    await agent_client.close()
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_skillnet_client(None)
        _set_skill_manager(None)
