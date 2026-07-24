# tests/test_permissions_e2e.py
"""End-to-end: chat.send -> ASK -> approval.respond -> complete, through the
real ws_handler + gateway MessageHandler + AgentClient, on a free port.
Uses a scripted LLM (no real API calls) + a registered echo tool (require-approval)."""
import asyncio

from websockets.asyncio.server import serve

from twinkle.agentserver.server import ws_handler, build_agent_loop
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.tools.decorator import tool
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


def test_full_approval_flow_through_gateway_and_agentserver(free_port, tmp_path, monkeypatch):
    monkeypatch.setenv("TWINKLE_PERMISSIONS", '{"enabled": true, "tools": {"echo": "require-approval"}}')
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import importlib, twinkle.config as cfg
    importlib.reload(cfg)

    @tool
    async def echo(text: str) -> str:
        """echo"""
        return f"tool-saw:{text}"

    scripted = _ScriptedLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    from twinkle.agentserver.sessions import session_store

    store = session_store()
    loop = build_agent_loop(store, llm=scripted)
    loop._tools.register(echo)  # echo isn't in the default tool_manager(); register it so execute("echo") works

    async def scenario():
        handler = ws_handler(loop, store)
        srv = await serve(handler, "127.0.0.1", free_port)
        try:
            ac = AgentClient(f"ws://127.0.0.1:{free_port}")
            await ac.connect()
            mh = MessageHandler(ac)
            # 1. inbound chat.send (R)
            msg = Message(id="R", type="req", channel_id="web", session_id="s1",
                          method="chat.send", params={"query": "call echo"})
            await mh.handle_message(msg)
            # 2. drain the approval.ask event (run_stream suspends right after yielding it, so only 1 event is queued)
            ask = await asyncio.wait_for(mh.dequeue_outbound(), timeout=10)
            assert ask.event_type is not None and ask.event_type.value == "approval.ask"
            aid = ask.payload["approval_id"]
            # 3. respond (R2)
            respond = Message(id="R2", type="req", channel_id="web", session_id="s1",
                              method="approval.respond",
                              params={"approval_id": aid, "decision": "allow",
                                      "original_request_id": "R"})
            await mh.handle_message(respond)
            # 4. drain the ack (result, R2) + the resumed chat.final (R)
            remaining = []
            for _ in range(2):
                remaining.append(await asyncio.wait_for(mh.dequeue_outbound(), timeout=10))
            kinds = [ask.event_type.value] + [e.event_type.value for e in remaining]
            assert "approval.ask" in kinds
            assert "result" in kinds        # ack for approval.respond
            assert "chat.final" in kinds   # resumed completion
            await ac.close()
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())
