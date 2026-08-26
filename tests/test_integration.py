"""端到端 Phase 1 集成:完整的 browser -> gateway -> agentserver ->
gateway -> browser 往返,由真实 AgentLoop 驱动,配一个 FAKE LLMClient(确定性、无需 API key)。

覆盖:流式分片、工具往返、跨轮 memory——即 roadmap 的 Phase 1 / M2 验收,无头运行。
"""
import asyncio
import json
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.agent import ReActAgent as AgentLoop
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
    """SkillNetClient 的免网络替身:预置的 catalog + 预置的下载 skill 目录。
    让 gateway 接缝的 e2e 不碰 GitHub 也能跑。真实 GitHub 覆盖在即抛型 ``_e2e_skillnet.py`` 里。"""
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
    """把 chat.delta 收集成 chat.final。返回 (assembled, saw_final)。"""
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
        # turn 1:模型调 echo 工具,然后作答
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
        server = await serve(ws_handler(loop_obj), "127.0.0.1", agentserver_port)
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
    """读帧直到 `result` 事件到达;跳过 delta/ack。5s 超时。"""
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
    """覆盖 session.list / session.create / history.get RPC 的完整 browser -> gateway -> AgentServer `result` 事件组帧。这些 RPC 不跑 ReAct 循环,故用一个空的 scripted LLM(无脚本)即可。"""
    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    async def run() -> None:
        # 预置一个 session,让 session.list 有东西可报
        await store.create_session("s-seed")
        await store.append(
            "s-seed", {"role": "user", "content": "hello"}, request_id="r0"
        )

        server = await serve(ws_handler(loop_obj), "127.0.0.1", agentserver_port)
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
    """覆盖 session.files + file.read RPC 的完整 browser -> gateway -> AgentServer ws 链路,断言 result 事件携带文件列表 + 内容。这些 RPC 不跑 ReAct 循环,故用一个空的 scripted LLM(无脚本)即可。"""
    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s-files"))
    asyncio.run(store.append("s-files", {"role": "user", "content": "hello"},
                              request_id="r0"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    async def run() -> None:
        server = await serve(ws_handler(loop_obj), "127.0.0.1", agentserver_port)
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
    """skills.search / skills.install / skills.list_local 的完整 browser -> gateway -> AgentServer 往返。验证 gateway 转发 skill RPC,且 install 后台 task 的延迟 e2a.result 一路解析成 browser `result` 事件 + 落盘 + list_local 能反映它。这些 RPC 不跑 ReAct 循环,故用一个空的 scripted LLM(无脚本)即可。
    免网络(FakeSkillNetClient);真实 GitHub 覆盖在 _e2e_skillnet.py。"""
    from twinkle.agentserver.skills import (
        _set_skill_manager, _set_skillnet_client, SkillManager,
    )
    from twinkle.agentserver.skills.remote import SkillNetSkill

    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo())

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # install 路径在调用时读 `twinkle.config.SKILLS_DIR`;list_local 读 SkillManager 单例。把两者都指向同一个 temp 目录,让 install 落盘后 list_local 能反映。
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    _set_skill_manager(SkillManager(str(skills_dir)))
    _set_skillnet_client(_FakeSkillNetClient(catalog=[
        SkillNetSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
    ]))
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", agentserver_port)
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

                        # skills.search(后台 task → 延迟 result)
                        await browser.send(json.dumps({
                            "type": "req", "id": "r-search",
                            "method": "skills.search",
                            "params": {"q": "foo", "session_id": "s1"},
                        }))
                        await _read_ack(browser)
                        payload = await _collect_result(browser)
                        assert payload["type"] == "skills.search"
                        assert [s["name"] for s in payload["skills"]] == ["foo"]

                        # skills.install(后台 task → 延迟 result + 落盘)
                        await browser.send(json.dumps({
                            "type": "req", "id": "r-install",
                            "method": "skills.install",
                            "params": {"url": "url_foo", "session_id": "s1"},
                        }))
                        await _read_ack(browser)
                        payload = await _collect_result(browser)
                        assert payload["ok"] is True
                        assert payload["skill_name"] == "foo"

                        # skills.list_local(内联 → 反映刚安装的 skill)
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
