# tests/test_permissions_e2e.py
"""端到端：chat.send -> ASK -> approval.respond -> complete，经真实 ws_handler + gateway MessageHandler + AgentClient，在 free port 上跑。
使用脚本化 LLM（无真实 API 调用）+ 注册的 echo tool（require-approval）。"""
import asyncio

from websockets.asyncio.server import serve

from twinkle.agentserver.server import ws_handler, create_agent
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
    monkeypatch.setenv("TWINKLE_WORKSPACE_DIR", str(tmp_path))
    import importlib, twinkle.config as cfg
    importlib.reload(cfg)
    # 通过 config 常量启用权限并注册 echo tool 的档位——permission_engine() 在
    # 调用时实时读取这些常量（对齐 test_file_tools 里 monkeypatch WORKSPACE_DIR 的
    # 做法）。TWINKLE_PERMISSIONS 环境变量在 v1 已移除。
    monkeypatch.setattr(cfg, "PERMISSIONS_ENABLED", True)
    monkeypatch.setattr(cfg, "PERMISSIONS_TOOLS",
                        {**cfg.PERMISSIONS_TOOLS, "echo": "require-approval"})

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
    from twinkle.agentserver.permissions import permission_engine
    from twinkle.agentserver.hooks.builtin import PermissionHook
    engine = permission_engine()
    loop = create_agent(store, hooks=[PermissionHook(engine)], llm=scripted)
    loop._tool_manager.register(echo)  # echo 不在默认 tool_manager() 中；在此注册以便 execute("echo") 可用

    async def scenario():
        handler = ws_handler(loop)
        srv = await serve(handler, "127.0.0.1", free_port)
        try:
            ac = AgentClient(f"ws://127.0.0.1:{free_port}")
            await ac.connect()
            mh = MessageHandler(ac)
            # 1. 入站 chat.send (R)
            msg = Message(id="R", type="req", channel_id="web", session_id="s1",
                          method="chat.send", params={"query": "call echo"})
            await mh.handle_message(msg)
            # 2. 取出 approval.ask event（run_stream 在 yield 它之后立即挂起，所以只入队 1 个 event）
            ask = await asyncio.wait_for(mh.dequeue_outbound(), timeout=10)
            assert ask.event_type is not None and ask.event_type.value == "approval.ask"
            aid = ask.payload["approval_id"]
            # 3. 响应 (R2)
            respond = Message(id="R2", type="req", channel_id="web", session_id="s1",
                              method="approval.respond",
                              params={"approval_id": aid, "decision": "allow",
                                      "original_request_id": "R"})
            await mh.handle_message(respond)
            # 4. 取出 ack（result，R2）+ 恢复后的 chat.final (R)
            remaining = []
            for _ in range(2):
                remaining.append(await asyncio.wait_for(mh.dequeue_outbound(), timeout=10))
            kinds = [ask.event_type.value] + [e.event_type.value for e in remaining]
            assert "approval.ask" in kinds
            assert "result" in kinds        # approval.respond 的 ack
            assert "chat.final" in kinds   # 恢复后的完成
            await ac.close()
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())
