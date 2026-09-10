"""Handler 级测试:畸形 envelope 仍报错;有效 envelope
到达注入的 loop。替代 tests/test_echo.py(echo 已移除)。"""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.server import ws_handler
from twinkle.agentserver.sessions import SessionStore
from twinkle.e2a.models import E2AEnvelope, E2AResponse


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RecordingLoop:
    """记录收到的 request,并流回一个预置 frame。"""
    def __init__(self, store=None):
        self.seen = None
        self.session_store = store

    async def run(self, request):
        self.seen = request
        yield E2AResponse(
            request_id=request.request_id,
            sequence=0,
            is_final=True,
            status="succeeded",
            response_kind="e2a.complete",
            body={"result": {"content": "ok"}},
        )


class _FakeSkillNetClient:
    """返回预置 catalog;让 ws 级测试能跑 search 分支,无需 GitHub。"""
    def __init__(self, catalog=None):
        self._catalog = catalog or []

    async def search_remote_skills(self, q, force_refresh=False):
        # 模拟服务端关键词匹配(真实 API 在 openkg 服务端搜,这里按 q 过滤 catalog)
        ql = (q or "").lower()
        return [s for s in self._catalog if not ql or ql in s.name.lower() or ql in s.description.lower()]


def test_malformed_envelope_returns_error(tmp_path) -> None:
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)

    async def run() -> None:
        server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                await ws.send("not-json-at-all")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_valid_envelope_dispatches_to_loop(tmp_path) -> None:
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)

    async def run() -> None:
        server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # connection.ack
                env = E2AEnvelope(
                    request_id="r1", session_id="s1", method="chat.send",
                    params={"query": "hi"},
                )
                await ws.send(env.model_dump_json())
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                assert data["response_kind"] == "e2a.complete"
                assert data["body"]["result"]["content"] == "ok"
            assert loop_obj.seen is not None
            assert loop_obj.seen.session_id == "s1"
            assert loop_obj.seen.query == "hi"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_skill_list_local_routes_inline(tmp_path) -> None:
    """skills.list_local 由 ws_handler 内联路由(永远到不了 ReAct loop)。"""
    from twinkle.agentserver.skills import _set_skill_manager, SkillManager
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)
    sk_dir = tmp_path / "skills" / "foo"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\nbody", encoding="utf-8")
    _set_skill_manager(SkillManager(str(tmp_path / "skills")))
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r1", session_id="s1", method="skills.list_local",
                        params={},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert [s["name"] for s in data["body"]["skills"]] == ["foo"]
                assert loop_obj.seen is None  # list_local short-circuits the ReAct loop
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_skill_manager(None)


def test_skill_search_runs_as_background_task(tmp_path) -> None:
    """skills.search 作为非内联后台 task 运行,延迟发一个 e2a.result。"""
    from twinkle.agentserver.skills import _set_skillnet_client
    from twinkle.agentserver.skills.remote import SkillNetSkill
    _set_skillnet_client(_FakeSkillNetClient(catalog=[
        SkillNetSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
        SkillNetSkill("bar", "a bar skill", "url_bar", "skills/bar/SKILL.md"),
    ]))
    try:
        port = _free_port()
        store = SessionStore(str(tmp_path / "sessions"))
        loop_obj = _RecordingLoop(store)
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r2", session_id="s2", method="skills.search",
                        params={"q": "foo"},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert data["body"]["type"] == "skills.search"
                    assert [s["name"] for s in data["body"]["skills"]] == ["foo"]
                assert loop_obj.seen is None  # search never reaches the ReAct loop
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_skillnet_client(None)


def test_skill_evolve_list_routes_inline(tmp_path) -> None:
    """skills.evolve_list 由 ws_handler 内联路由(改前走后台分支报 unknown skill method)。"""
    from twinkle.agentserver.evolution import EvolutionStore, _set_evolution_store
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)
    sk_dir = tmp_path / "skills" / "foo"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    _set_evolution_store(EvolutionStore(str(tmp_path / "skills")))
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r1", session_id="s1", method="skills.evolve_list",
                        params={"name": "foo"},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert data["body"]["type"] == "skills.evolve_list"
                    assert data["body"]["skill_name"] == "foo"
                    assert data["body"]["records"] == []
                assert loop_obj.seen is None  # 内联短路,不到 ReAct loop
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_evolution_store(None)


def test_skill_evolve_pending_routes_inline(tmp_path) -> None:
    """skills.evolve_pending 由 ws_handler 内联路由。"""
    from twinkle.agentserver.evolution import (
        EvolutionStore, OnlineEvolutionOrchestrator, ConversationSignalDetector,
        _set_evolution_store, _set_orchestrator,
    )
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)
    evo_store = EvolutionStore(str(tmp_path / "skills"))
    orch = OnlineEvolutionOrchestrator(
        store=evo_store, optimizer=None, scorer=None,
        detector=ConversationSignalDetector())
    _set_evolution_store(evo_store)
    _set_orchestrator(orch)
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r2", session_id="s2", method="skills.evolve_pending",
                        params={},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert data["body"]["type"] == "skills.evolve_pending"
                    assert data["body"]["pending"] == {}
                assert loop_obj.seen is None
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_evolution_store(None)
        _set_orchestrator(None)
