import asyncio
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from app.config import settings

log = logging.getLogger("telegram-ai-news.persistence")

_state = {
    "persistent": False,
    "root": "",
    "database": "",
    "backup": "",
    "mode": "ephemeral",
}
_backup_task: asyncio.Task | None = None


def _mounted_volume_root() -> Path | None:
    configured = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if configured:
        root = Path(configured)
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except Exception:
            log.exception("Cannot prepare Railway volume root %s", configured)

    # Some Railway deployments expose /data as the mounted volume but do not
    # populate RAILWAY_VOLUME_MOUNT_PATH in every runtime context.
    data = Path("/data")
    try:
        if data.exists() and os.path.ismount(str(data)):
            return data
    except Exception:
        pass
    return None


def install_persistent_database_path() -> dict:
    """Move the SQLite database onto the Railway Volume before DB access starts."""
    root = _mounted_volume_root()
    explicit = (os.getenv("DATABASE_PATH") or "").strip()
    current = Path(settings.database_path)

    if root is None:
        _state.update(
            persistent=False,
            root="",
            database=str(current),
            backup="",
            mode="ephemeral",
        )
        log.warning("Persistent Railway volume not detected; database=%s", current)
        return dict(_state)

    # Respect a custom absolute DATABASE_PATH only when it is already inside the
    # mounted volume. Relative/default paths are transparently promoted to /data.
    if explicit:
        candidate = Path(explicit)
        try:
            inside_volume = candidate.is_absolute() and str(candidate.resolve()).startswith(str(root.resolve()) + os.sep)
        except Exception:
            inside_volume = False
    else:
        inside_volume = False

    database = Path(explicit) if inside_volume else root / "sports_news.db"
    backup = root / "backups" / "sports_news.latest.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)

    # First persistent boot: preserve the existing ephemeral/legacy DB if it has
    # data. Otherwise restore the last durable snapshot when available.
    if not database.exists():
        legacy_candidates = [current, Path("data/news.db")]
        copied = False
        for legacy in legacy_candidates:
            try:
                if legacy.exists() and legacy.resolve() != database.resolve() and legacy.stat().st_size > 0:
                    shutil.copy2(legacy, database)
                    copied = True
                    log.info("Migrated legacy SQLite DB %s -> %s", legacy, database)
                    break
            except Exception:
                log.exception("Legacy DB migration failed from %s", legacy)
        if not copied and backup.exists():
            try:
                shutil.copy2(backup, database)
                log.info("Restored SQLite DB from durable backup %s", backup)
            except Exception:
                log.exception("Could not restore SQLite backup %s", backup)

    settings.database_path = str(database)
    _state.update(
        persistent=True,
        root=str(root),
        database=str(database),
        backup=str(backup),
        mode="railway-volume",
    )
    log.info("Persistent storage active database=%s", database)
    return dict(_state)


def _backup_database(source: Path, destination: Path) -> None:
    if not source.exists() or source.stat().st_size <= 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp")
    if temp.exists():
        temp.unlink(missing_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(temp), timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    temp.replace(destination)


async def prepare_persistence() -> dict:
    state = install_persistent_database_path()
    from app.database import init_db

    await init_db()
    # WAL keeps readers/writers responsive while the bot, Mini App and publisher
    # share the same durable database.
    try:
        import aiosqlite
        async with aiosqlite.connect(settings.database_path, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=FULL")
            await db.execute("PRAGMA busy_timeout=30000")
            await db.commit()
    except Exception:
        log.exception("Could not apply SQLite durability pragmas")
    return state


async def _backup_loop() -> None:
    while True:
        try:
            if _state.get("persistent") and _state.get("backup"):
                await asyncio.to_thread(
                    _backup_database,
                    Path(settings.database_path),
                    Path(str(_state["backup"])),
                )
                log.info("Durable SQLite snapshot refreshed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Durable SQLite snapshot failed")
        await asyncio.sleep(1800)


def start_persistence_backup_worker() -> asyncio.Task | None:
    global _backup_task
    if not _state.get("persistent"):
        return None
    if _backup_task is None or _backup_task.done():
        _backup_task = asyncio.create_task(_backup_loop(), name="sqlite-durable-backup")
    return _backup_task


def storage_status() -> dict:
    return {
        "persistent": bool(_state.get("persistent")),
        "mode": str(_state.get("mode") or "ephemeral"),
        "database": Path(str(_state.get("database") or settings.database_path)).name,
        "backup_enabled": bool(_state.get("persistent") and _state.get("backup")),
    }
