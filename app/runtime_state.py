from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


_STARTED_MONOTONIC = time.monotonic()
_STARTED_AT = datetime.now(timezone.utc)
_services: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark(name: str, status: str, *, detail: str = "", **extra: Any) -> None:
    item = {
        "name": name,
        "status": str(status),
        "detail": str(detail or ""),
        "updated_at": _now_iso(),
    }
    item.update(extra)
    _services[name] = item


def track_task(name: str, task: asyncio.Task, *, detail: str = "") -> asyncio.Task:
    _tasks[name] = task
    mark(name, "running", detail=detail)

    def _done(done: asyncio.Task) -> None:
        if done.cancelled():
            mark(name, "stopped", detail="cancelled")
            return
        error = done.exception()
        if error is None:
            mark(name, "stopped", detail="worker exited")
        else:
            mark(name, "error", detail=f"{type(error).__name__}: {error}"[:300])

    task.add_done_callback(_done)
    return task


def task(name: str) -> asyncio.Task | None:
    return _tasks.get(name)


def snapshot() -> dict[str, Any]:
    services = {key: dict(value) for key, value in _services.items()}
    for name, worker in list(_tasks.items()):
        current = services.get(name, {"name": name})
        if worker.cancelled():
            current["status"] = "stopped"
            current["detail"] = current.get("detail") or "cancelled"
        elif worker.done():
            try:
                error = worker.exception()
            except asyncio.CancelledError:
                error = None
            current["status"] = "error" if error else "stopped"
            if error:
                current["detail"] = f"{type(error).__name__}: {error}"[:300]
        else:
            current["status"] = "running"
        services[name] = current

    uptime = max(0, int(time.monotonic() - _STARTED_MONOTONIC))
    healthy = all(
        item.get("status") not in {"error", "offline"}
        for item in services.values()
    )
    return {
        "healthy": healthy,
        "started_at": _STARTED_AT.isoformat(timespec="seconds"),
        "uptime_seconds": uptime,
        "services": services,
    }


def compact_snapshot() -> dict[str, Any]:
    data = snapshot()
    return {
        "healthy": data["healthy"],
        "uptime_seconds": data["uptime_seconds"],
        "services": {
            name: {
                "status": item.get("status", "unknown"),
                "detail": item.get("detail", ""),
            }
            for name, item in data["services"].items()
        },
    }
