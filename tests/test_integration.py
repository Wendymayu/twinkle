"""End-to-end Phase 0 integration test: the full browser -> gateway ->
agentserver -> gateway -> browser round trip, through WebChannel +
MessageHandler + ChannelManager + AgentClient.

This is the roadmap Phase 0 acceptance criterion (echo streamed back through
both processes), exercised headlessly.
"""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from twinkle.agentserver.server import handler as agentserver_handler
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel


def test_end_to_end_echo(port_factory) -> None:
    agentserver_port = port_factory()
    gateway_port = port_factory()

    async def run() -> None:
        agentserver = await serve(
            agentserver_handler, "127.0.0.1", agentserver_port
        )
        try:
            # build the gateway wiring in-process
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()

            channel_manager = ChannelManager()
            message_handler = MessageHandler(agent_client, channel_manager)
            channel_manager.set_message_handler(message_handler)

            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()

            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    # consume connection.ack
                    await browser.recv()

                    await browser.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": "r1",
                                "method": "chat.send",
                                "params": {"query": "hello"},
                            }
                        )
                    )

                    # first inbound frame must be the immediate res ACK
                    ack = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
                    assert ack["type"] == "res"
                    assert ack["id"] == "r1"
                    assert ack["ok"] is True

                    # then collect streamed chat.delta events until chat.final
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
                    assert saw_final, "never received chat.final"
                    assert assembled == "Echo: hello"
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            agentserver.close()
            await agentserver.wait_closed()

    asyncio.run(run())
