"""CronJobStore —— 单文件 cron_jobs.json 持久化，带原子写。

被 gateway scheduler（读 + mtime 轮询）和 agent cron 工具（写）共享。
asyncio.Lock 守护并发；写操作是 .tmp + os.replace，崩溃也不会留下半写文件。
单条坏 job 行被跳过（非致命）。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from twinkle.config import WORKSPACE_DIR
from twinkle.gateway.cron.models import CronJob

_VERSION = 1


def default_cron_jobs_path() -> Path:
    """<WORKSPACE_DIR>/cron_jobs.json —— gateway 与 agent 工具共享。"""
    return Path(WORKSPACE_DIR) / "cron_jobs.json"


def default_sidecar_path() -> Path:
    """<WORKSPACE_DIR>/cron_trigger_now.json —— run_now sidecar（agent→gateway）。"""
    return default_cron_jobs_path().parent / "cron_trigger_now.json"


class CronJobStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    # --- 内部 IO ---
    def _read_unlocked(self) -> dict:
        if not self._path.exists():
            return {"version": _VERSION, "jobs": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": _VERSION, "jobs": []}
        jobs = []
        for raw in data.get("jobs", []):
            try:
                jobs.append(CronJob.from_dict(raw).to_dict())
            except Exception:
                continue  # 跳过坏 job
        return {"version": _VERSION, "jobs": jobs}

    def _write_unlocked(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, self._path)

    # --- CRUD ---
    async def list_jobs(self) -> list[CronJob]:
        async with self._lock:
            data = self._read_unlocked()
        jobs = [CronJob.from_dict(j) for j in data["jobs"]]
        jobs.sort(key=lambda j: j.updated_at or j.created_at or 0, reverse=True)
        return jobs

    async def get_job(self, job_id: str) -> CronJob | None:
        data = self._read_unlocked()  # 读无需持锁
        for j in data["jobs"]:
            if j.get("id") == job_id:
                return CronJob.from_dict(j)
        return None

    async def create_job(self, fields: dict) -> CronJob:
        async with self._lock:
            data = self._read_unlocked()
            now = time.time()
            job = CronJob.from_dict({
                **fields,
                "id": _new_id(),
                "created_at": now, "updated_at": now,
            })
            # 往返校验
            CronJob.from_dict(job.to_dict())
            data["jobs"].append(job.to_dict())
            self._write_unlocked(data)
            return job

    async def update_job(self, job_id: str, patch: dict) -> CronJob:
        async with self._lock:
            data = self._read_unlocked()
            for i, j in enumerate(data["jobs"]):
                if j.get("id") == job_id:
                    # id/created_at 不可变：防止改身份导致堆事件(keyed by old id)
                    # orphan + _jobs 错乱
                    safe_patch = {k: v for k, v in patch.items()
                                  if k not in ("id", "created_at")}
                    merged = {**j, **safe_patch, "updated_at": time.time()}
                    # 重新 enable 或改 cron_expr → 清 expired
                    if safe_patch.get("enabled") is True or "cron_expr" in safe_patch:
                        merged["expired"] = False
                    job = CronJob.from_dict(merged)
                    CronJob.from_dict(job.to_dict())
                    data["jobs"][i] = job.to_dict()
                    self._write_unlocked(data)
                    return job
            raise KeyError(f"cron job not found: {job_id}")

    async def delete_job(self, job_id: str) -> bool:
        async with self._lock:
            data = self._read_unlocked()
            before = len(data["jobs"])
            data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
            if len(data["jobs"]) == before:
                return False
            self._write_unlocked(data)
            return True


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex
