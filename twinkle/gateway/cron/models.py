"""Cron data models — CronJob (persisted) / CronRunState (in-memory) / _Event (heap node).

CronJob is persisted to cron_jobs.json; CronRunState tracks a single run in
memory (not persisted — reload() rebuilds future events, in-flight run state
is lost on restart, see spec §8 known limitation). _Event is the min-heap node.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CronJob:
    id: str
    name: str
    cron_expr: str
    timezone: str
    enabled: bool = True
    wake_offset_seconds: int = 60
    description: str = ""
    expired: bool = False
    targets: str = "web"
    delete_after_run: bool = False
    created_at: float | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "cron_expr": self.cron_expr,
            "timezone": self.timezone, "enabled": self.enabled,
            "wake_offset_seconds": self.wake_offset_seconds,
            "description": self.description, "expired": self.expired,
            "targets": self.targets, "delete_after_run": self.delete_after_run,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CronJob":
        for req in ("id", "name", "cron_expr", "timezone"):
            if not d.get(req):
                raise ValueError(f"cron job missing required field: {req}")
        return cls(
            id=d["id"], name=d["name"], cron_expr=d["cron_expr"],
            timezone=d["timezone"], enabled=d.get("enabled", True),
            wake_offset_seconds=d.get("wake_offset_seconds", 60),
            description=d.get("description", ""),
            expired=d.get("expired", False),
            targets=d.get("targets", "web"),
            delete_after_run=d.get("delete_after_run", False),
            created_at=d.get("created_at"), updated_at=d.get("updated_at"),
        )


@dataclass
class CronRunState:
    run_id: str
    job_id: str
    wake_at_iso: str
    push_at_iso: str
    status: str = "pending"  # pending | running | succeeded | failed
    placeholder_sent: bool = False
    pushed_final: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    result_text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _Event:
    at_ts: float
    seq: int
    kind: str  # wake | push | push_update
    job_id: str
    run_id: str
