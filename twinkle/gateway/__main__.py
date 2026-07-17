"""Gateway entry point: `python -m twinkle.gateway`.

Wires AgentClient -> MessageHandler -> ChannelManager -> WebChannel and runs
the two async servers (browser ws + agentserver client) in one process.
"""
import asyncio
import logging

from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, GATEWAY_HOST, GATEWAY_PORT
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main() -> None:
    agent_client = AgentClient(f"ws://{AGENTSERVER_HOST}:{AGENTSERVER_PORT}")
    await agent_client.connect()

    channel_manager = ChannelManager()
    message_handler = MessageHandler(agent_client, channel_manager)
    channel_manager.set_message_handler(message_handler)

    web_channel = WebChannel(GATEWAY_HOST, GATEWAY_PORT)
    channel_manager.register_channel(web_channel)

    await channel_manager.start()
    # runs forever (WebChannel.start blocks on asyncio.Future)
    await web_channel.start()


if __name__ == "__main__":
    asyncio.run(main())
