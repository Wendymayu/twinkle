"""End-to-end Phase 1 integration: the full browser -> gateway ->
agentserver -> gateway -> browser round trip, driven by a REAL AgentLoop
with a FAKE LLMClient (deterministic, no API key).

Exercises: streaming chunks, tool round-trip, and cross-turn memory —
the roadmap Phase 1 / M2 acceptance, headlessly.
"""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.agent_loop import AgentLoop
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory import LongTermMemory
from twinkle.agentserver.server import ws_handler
from twinkle.agentserver.session_store import SessionStore
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
    loop_obj = AgentLoop(_ScriptedLLM(scripts), store, _reg_with_echo(), LongTermMemory())

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
