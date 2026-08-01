"""cron_tools: 5 @tool wrappers over CronJobStore (+ run_now sidecar)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from twinkle.agentserver.tools.builtin import cron_tools
from twinkle.gateway.cron.store import CronJobStore


def run(coro):
    return asyncio.run(coro)


def _isolated_store(tmp_path, monkeypatch):
    """把 cron_tools 指向 tmp 下的 cron_jobs.json + sidecar。"""
    path = tmp_path / "cron_jobs.json"
    sidecar = tmp_path / "cron_trigger_now.json"
    monkeypatch.setattr(cron_tools, "_store", CronJobStore(path))
    monkeypatch.setattr(cron_tools, "_sidecar_path", sidecar)
    return path


def test_cron_create_and_list(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    out = run(cron_tools.cron_create_job.invoke(
        {"name": "日报", "cron_expr": "0 9 * * *", "timezone": "Asia/Shanghai",
         "description": "生成日报"}))
    assert "日报" in out
    listing = run(cron_tools.cron_list_jobs.invoke({}))
    assert "日报" in listing


def test_cron_list_jobs_shows_next_run(tmp_path, monkeypatch):
    """Fix 6: cron_list_jobs 每条任务展示下次 push 时间（next=ISO）。"""
    _isolated_store(tmp_path, monkeypatch)
    run(cron_tools.cron_create_job.invoke(
        {"name": "日报", "cron_expr": "0 9 * * *", "timezone": "Asia/Shanghai",
         "description": "生成日报"}))
    listing = run(cron_tools.cron_list_jobs.invoke({}))
    assert "next=" in listing
    # 合法循环 expr → 应是 ISO 时间（含 T），不是 已过期/无效
    assert "next=已过期" not in listing
    assert "next=无效" not in listing
    # ISO 形如 2026-07-30T09:00:00+08:00
    import re
    assert re.search(r"next=\d{4}-\d{2}-\d{2}T", listing), listing


def test_cron_list_jobs_expired_shows_expired(tmp_path, monkeypatch):
    """Fix 6: 过期单次任务展示 next=已过期。"""
    _isolated_store(tmp_path, monkeypatch)
    run(cron_tools.cron_create_job.invoke(
        {"name": "old", "cron_expr": "0 9 1 1 * 0 2020", "timezone": "UTC"}))
    listing = run(cron_tools.cron_list_jobs.invoke({}))
    assert "next=已过期" in listing


def test_cron_update(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    created = run(cron_tools.cron_create_job.invoke(
        {"name": "x", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    jid = _extract_id(created)
    out = run(cron_tools.cron_update_job.invoke(
        {"job_id": jid, "fields": '{"description": "改了"}'}))
    assert "改了" in out


def test_cron_delete(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    created = run(cron_tools.cron_create_job.invoke(
        {"name": "y", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    jid = _extract_id(created)
    out = run(cron_tools.cron_delete_job.invoke({"job_id": jid}))
    assert "已删除" in out or "deleted" in out.lower()


def test_cron_run_now_writes_sidecar(tmp_path, monkeypatch):
    path = _isolated_store(tmp_path, monkeypatch)
    sidecar = tmp_path / "cron_trigger_now.json"
    created = run(cron_tools.cron_create_job.invoke(
        {"name": "z", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    jid = _extract_id(created)
    run(cron_tools.cron_run_now.invoke({"job_id": jid}))
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["job_id"] == jid


def _extract_id(create_output: str) -> str:
    # create 工具输出含 job id；格式 "created <id> ..."
    return create_output.split()[1]
