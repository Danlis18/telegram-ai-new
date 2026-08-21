import hashlib
from pathlib import Path

import aiosqlite

from app.config import settings


async def _ensure_column(db, name: str, definition: str) -> None:
    cur = await db.execute("PRAGMA table_info(news)")
    columns = {row[1] for row in await cur.fetchall()}
    if name not in columns:
        await db.execute(f"ALTER TABLE news ADD COLUMN {name} {definition}")


async def init_db():
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            original_text TEXT,
            rewritten_text TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'received',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await _ensure_column(db, "media_type", "TEXT")
        await _ensure_column(db, "media_file_id", "TEXT")
        await _ensure_column(db, "original_media_file_id", "TEXT")
        await _ensure_column(db, "scheduled_at", "DATETIME")
        await _ensure_column(db, "published_at", "DATETIME")

        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS editorial_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER,
            original_text TEXT NOT NULL,
            ai_text TEXT NOT NULL,
            corrected_text TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS generation_stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS custom_image_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            image_blob BLOB NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()


def fingerprint(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


async def seen(text: str) -> bool:
    fp = fingerprint(text)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT 1 FROM news WHERE fingerprint=? LIMIT 1", (fp,))
        return await cur.fetchone() is not None


async def save(source: str, message_id: int, original: str, rewritten: str, score: int, status: str) -> int | None:
    fp = fingerprint(original)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO news (fingerprint,source,source_message_id,original_text,rewritten_text,score,status) VALUES (?,?,?,?,?,?,?)",
            (fp, source, message_id, original, rewritten, score, status),
        )
        await db.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        cur = await db.execute("SELECT id FROM news WHERE fingerprint=? LIMIT 1", (fp,))
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def get_setting(key: str, default: str | None = None) -> str | None:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def increment_generation_stat(key: str, amount: int = 1) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO generation_stats (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=value+excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (key, int(amount)),
        )
        await db.commit()


async def get_generation_stats() -> dict[str, int]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT key,value FROM generation_stats")
        return {str(row[0]): int(row[1]) for row in await cur.fetchall()}


async def save_custom_template(name: str, image_bytes: bytes) -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "INSERT INTO custom_image_templates (name,image_blob) VALUES (?,?)",
            (name.strip()[:120] or "Template", image_bytes),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_custom_templates(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,name,created_at FROM custom_image_templates ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def get_custom_template(template_id: int) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,name,image_blob,created_at FROM custom_image_templates WHERE id=?",
            (template_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_custom_template(template_id: int) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("DELETE FROM custom_image_templates WHERE id=?", (template_id,))
        await db.commit()


async def get_news(news_id: int):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM news WHERE id=?", (news_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_news(news_id: int, **fields) -> None:
    allowed = {
        "rewritten_text",
        "score",
        "status",
        "media_type",
        "media_file_id",
        "original_media_file_id",
        "scheduled_at",
        "published_at",
    }
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    sql = "UPDATE news SET " + ", ".join(f"{k}=?" for k, _ in items) + " WHERE id=?"
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(sql, [v for _, v in items] + [news_id])
        await db.commit()


async def save_editorial_feedback(news_id: int, original_text: str, ai_text: str, corrected_text: str) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO editorial_feedback (news_id,original_text,ai_text,corrected_text) VALUES (?,?,?,?)",
            (news_id, original_text, ai_text, corrected_text),
        )
        await db.commit()


async def get_style_examples(limit: int = 6) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT original_text, ai_text, corrected_text FROM editorial_feedback ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_feedback_count() -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM editorial_feedback")
        return int((await cur.fetchone())[0])


async def get_queue(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status='ready' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_scheduled(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL ORDER BY scheduled_at ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_due_scheduled(now_utc: str, limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=? ORDER BY scheduled_at ASC LIMIT ?",
            (now_utc, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_archive(limit: int = 15):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status IN ('published','skipped','rejected','ai_error','raw','advertising') ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_recent(limit: int = 12):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def stats():
    async with aiosqlite.connect(settings.database_path) as db:
        result = {}
        for key, where in {
            "total": "1=1",
            "today": "date(created_at)=date('now')",
            "ready": "status='ready'",
            "scheduled": "status='scheduled'",
            "published": "status='published'",
            "rejected": "status='rejected'",
            "skipped": "status='skipped'",
            "received": "status='received'",
            "ai_error": "status='ai_error'",
            "raw": "status='raw'",
            "advertising": "status='advertising'",
        }.items():
            cur = await db.execute(f"SELECT COUNT(*) FROM news WHERE {where}")
            result[key] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(AVG(score),0) FROM news WHERE score > 0")
        result["avg_score"] = round((await cur.fetchone())[0], 1)
        return result
