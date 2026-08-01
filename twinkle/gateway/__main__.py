"""Gateway entry point: `python -m twinkle.gateway`.

Wires AgentClient -> MessageHandler -> ChannelManager -> WebChannel and runs
the two async servers (browser ws + agentserver client) in one process.

Dependency direction (aligned with jiuwenclaw, unidirectional):
  MessageHandler(agent_client)                — only knows AgentClient
  ChannelManager(message_handler)             — knows MessageHandler (inbound + outbound Queue)
No circular reference at all.
"""
import asyncio

from twinkle.config import AGENTSERVER_HOST, AGENTSERVER_PORT, GATEWAY_HOST, GATEWAY_PORT
from twinkle.gateway.agent_client import AgentClient
from twinkle.gateway.channel_manager import ChannelManager
from twinkle.gateway.message_handler import MessageHandler
from twinkle.gateway.web_channel import WebChannel
from twinkle.logging_config import setup_logging


async def main() -> None:
    agent_client = AgentClient(f"ws://{AGENTSERVER_HOST}:{AGENTSERVER_PORT}")
    await agent_client.connect()

    message_handler = MessageHandler(agent_client)
    channel_manager = ChannelManager(message_handler)

    web_channel = WebChannel(GATEWAY_HOST, GATEWAY_PORT)
    channel_manager.register_channel(web_channel)

    from twinkle.gateway.cron.scheduler import CronSchedulerService
    from twinkle.gateway.cron.store import CronJobStore, default_cron_jobs_path
    from twinkle.workspace import ensure_workspace_dir
    ensure_workspace_dir()
    cron_store = CronJobStore(default_cron_jobs_path())
    cron_scheduler = CronSchedulerService(
        store=cron_store, agent_client=agent_client,
        message_handler=message_handler,
    )
    await cron_scheduler.start()

    await channel_manager.start()
    # runs forever (WebChannel.start blocks on asyncio.Future)
    await web_channel.start()


if __name__ == "__main__":
    setup_logging("gateway")
    asyncio.run(main())
