"""Gateway-side cron: scheduler + store + models + cron expr.

Mirrors jiuwenclaw/gateway/cron/. CronSchedulerService is the clock; it
schedules wake/push/push_update events on a min-heap and drives them via
asyncio. AgentServer is channel-agnostic (no cron awareness)."""
