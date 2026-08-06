"""Cron agent tools — 5 @tool wrappers over CronJobStore.

list/create/update/delete/run_now. All write the shared <workspace>/cron_jobs.json
(gateway scheduler hot-reloads via mtime). run_now writes a sidecar file
(<workspace>/cron_trigger_now.json) because there is no reverse WS channel
(AgentServer -> Gateway); the gateway _loop detects it and calls
trigger_run_now, then deletes the sidecar. Errors are returned as strings
(never raised) so a bad call doesn't crash ReAct — mirrors memory_tools.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from twinkle.agentserver.tools.decorator import tool
from twinkle.gateway.cron.models import CronJob
from twinkle.gateway.cron.store import (
    CronJobStore,
    default_cron_jobs_path,
    default_sidecar_path,
)

# 进程级单例（agent 进程）；测试用 monkeypatch 替换
_store: CronJobStore = CronJobStore(default_cron_jobs_path())
_sidecar_path: Path = default_sidecar_path()


def _next_run_iso(job: CronJob) -> str:
    """计算 job 下次 push 的 ISO 时间；过期/无效时返回中文标记。"""
    from zoneinfo import ZoneInfo

    from twinkle.gateway.cron import cron_expr as ce
    try:
        tz = ZoneInfo(job.timezone)
        push_dt = ce._cron_next_push_dt(job.cron_expr, datetime.now(tz=tz))
        return push_dt.isoformat()
    except Exception as exc:
        if ce._is_croniter_no_next_date(exc):
            return "已过期"
        return "无效"


@tool
async def cron_list_jobs() -> str:
    """列出所有定时任务(cron job)及其下次执行时间。当用户问'有哪些定时任务'时调用。"""
    jobs = await _store.list_jobs()
    if not jobs:
        return "暂无定时任务。"
    lines = [f"## 定时任务 ({len(jobs)} 条)"]
    for job in jobs:
        state = "启用" if job.enabled else "禁用"
        if job.expired:
            state = "已过期"
        lines.append(f"- [{job.id}] {job.name} | cron={job.cron_expr} tz={job.timezone} "
                     f"| {state} | next={_next_run_iso(job)} | {job.description or '无描述'}")
    return "\n".join(lines)


@tool
async def cron_create_job(name: str, cron_expr: str, timezone: str,
                          description: str = "", wake_offset_seconds: int = 60,
                          targets: str = "web", delete_after_run: bool = False,
                          enabled: bool = True) -> str:
    """创建一个定时任务。cron_expr: 5-field(循环)或7-field(单次含秒+年); timezone: IANA如Asia/Shanghai; description: 喂给agent的任务指令。"""
    try:
        from twinkle.gateway.cron import cron_expr as ce
        ce.validate_cron_expression(cron_expr, timezone)
    except Exception as exc:
        return f"创建失败: {exc}"
    job = await _store.create_job({
        "name": name, "cron_expr": cron_expr, "timezone": timezone,
        "description": description, "wake_offset_seconds": wake_offset_seconds,
        "targets": targets, "delete_after_run": delete_after_run, "enabled": enabled,
    })
    return f"created {job.id} name={job.name} cron={job.cron_expr}"


@tool
async def cron_update_job(job_id: str, fields: str) -> str:
    """更新定时任务字段。fields: JSON字符串,如{"description":"新描述","enabled":false}。重新启用或改cron_expr会清除过期标记。"""
    try:
        patch = json.loads(fields)
    except Exception as exc:
        return f"fields 不是合法 JSON: {exc}"
    try:
        job = await _store.update_job(job_id, patch)
    except KeyError:
        return f"未找到任务: {job_id}"
    return f"updated {job.id} name={job.name} | {patch}"


@tool
async def cron_delete_job(job_id: str) -> str:
    """删除一个定时任务。"""
    ok = await _store.delete_job(job_id)
    return f"已删除 {job_id}" if ok else f"未找到任务: {job_id}"


@tool
async def cron_run_now(job_id: str) -> str:
    """立即触发一个定时任务(不等到点)。写入触发信号,gateway调度器检测后立即执行。"""
    job = await _store.get_job(job_id)
    if job is None:
        return f"未找到任务: {job_id}"
    _sidecar_path.write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
    return f"triggered {job_id} ({job.name}) — gateway 将立即执行"
