"""Gateway 入口：`python -m twinkle.gateway`。

串联 AgentClient -> MessageHandler -> ChannelManager -> WebChannel，在同一进程里
跑两个 async server（浏览器 ws + agentserver client）。另启动 CronSchedulerService
（gateway 侧 cron 时钟）。

依赖方向（对齐 jiuwenclaw，单向）：
  MessageHandler(agent_client)                — 只持有 AgentClient
  ChannelManager(message_handler)             — 持有 MessageHandler（inbound + outbound Queue）
完全没有循环引用。
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
    # 永久运行（WebChannel.start 阻塞在 asyncio.Future 上）
    await web_channel.start()


if __name__ == "__main__":
    setup_logging("gateway")
    asyncio.run(main())
