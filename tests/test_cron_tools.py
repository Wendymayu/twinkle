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
