"""Gateway 侧 cron：scheduler + store + models + cron expr。

镜像 jiuwenclaw/gateway/cron/。CronSchedulerService 是时钟；它在 min-heap 上
调度 wake/push/push_update 事件并通过 asyncio 驱动。AgentServer 与 channel 无关
（不感知 cron）。"""
