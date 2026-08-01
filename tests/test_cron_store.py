"""CronJobStore: single-file CRUD + atomic write + mtime."""
from __future__ import annotations

import asyncio
from pathlib import Path

from twinkle.gateway.cron.models import CronJob
from twinkle.gateway.cron.store import CronJobStore


def run(coro):
    return asyncio.run(coro)


def test_create_and_get(tmp_path):
    store = CronJobStore(tmp_path / "cron_jobs.json")
    j = run(store.create_job({"name": "日报", "cron_expr": "0 9 * * *",
                              "timezone": "Asia/Shanghai",
                              "description": "生成日报"}))
    assert j.id and j.name == "日报"
    got = run(store.get_job(j.id))
    assert got is not None and got.id == j.id


def test_list_orders_by_updated_desc(tmp_path):
    store = CronJobStore(tmp_path / "c.json")
    a = run(store.create_job({"name": "a", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    b = run(store.create_job({"name": "b", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    run(store.update_job(a.id, {"description": "updated a"}))
    jobs = run(store.list_jobs())
    assert jobs[0].id == a.id  # a 更新更晚 → 排前


def test_update_reenable_clears_expired(tmp_path):
    store = CronJobStore(tmp_path / "c.json")
    j = run(store.create_job({"name": "x", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    run(store.update_job(j.id, {"enabled": False, "expired": True}))
    j2 = run(store.update_job(j.id, {"enabled": True}))
    assert j2.expired is False


def test_update_cannot_change_id(tmp_path):
    """Fix 4: update_job 不得让调用方改 id/created_at（会 orphan 堆事件、错乱 _jobs）。"""
    store = CronJobStore(tmp_path / "c.json")
    j = run(store.create_job({"name": "x", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    original_id = j.id
    original_created = j.created_at
    j2 = run(store.update_job(j.id, {
        "id": "hacked", "created_at": 999.0, "description": "改了",
    }))
    assert j2.id == original_id          # id 不变
    assert j2.created_at == original_created  # created_at 不变
    assert j2.description == "改了"       # 其他字段可改
    # 持久层也不应被改
    got = run(store.get_job(j.id))
    assert got.id == original_id and got.created_at == original_created


def test_delete(tmp_path):
    store = CronJobStore(tmp_path / "c.json")
    j = run(store.create_job({"name": "x", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    assert run(store.delete_job(j.id)) is True
    assert run(store.get_job(j.id)) is None


def test_atomic_write_no_partial_on_crash(tmp_path):
    """写时 .tmp 先写再 rename；中途崩溃不留半文件。"""
    store = CronJobStore(tmp_path / "c.json")
    run(store.create_job({"name": "x", "cron_expr": "0 9 * * *", "timezone": "UTC"}))
    # 文件存在且可解析；无残留 .tmp
    assert (tmp_path / "c.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_bad_single_job_skipped_not_crash(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"version":1,"jobs":[{"id":"bad"}]}', encoding="utf-8")  # 缺字段
    store = CronJobStore(p)
    jobs = run(store.list_jobs())
    assert jobs == []  # 坏 job 跳过，不崩


def test_default_cron_jobs_path_under_workspace():
    from twinkle.gateway.cron.store import default_cron_jobs_path
    p = default_cron_jobs_path()
    assert p.name == "cron_jobs.json"


def test_default_sidecar_path_under_workspace():
    """Fix 8: default_sidecar_path 与 cron_jobs 同目录。"""
    from twinkle.gateway.cron.store import default_cron_jobs_path, default_sidecar_path
    sp = default_sidecar_path()
    assert sp.name == "cron_trigger_now.json"
    assert sp.parent == default_cron_jobs_path().parent
