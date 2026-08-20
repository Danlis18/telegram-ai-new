import hashlib
from pathlib import Path
import aiosqlite
from app.config import settings


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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            published_at DATETIME
        )""")
        try:
            await db.execute("ALTER TABLE news ADD COLUMN published_at DATETIME")
        except Exception:
            pass
        await db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('processing_paused','false')")
        await db.commit()


def fingerprint(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


async def seen(text: str) -> bool:
    fp = fingerprint(text)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT 1 FROM news WHERE fingerprint=? LIMIT 1", (fp,))
        return await cur.fetchone() is not None


async def save(source: str, message_id: int, original: str, rewritten: str, score: int, status: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO news (fingerprint,source,source_message_id,original_text,rewritten_text,score,status) VALUES (?,?,?,?,?,?,?)",
            (fingerprint(original), source, message_id, original, rewritten, score, status),
        )
        await db.commit()


async def get_news(news_id: int):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM news WHERE id=?", (news_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_queue(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status='ready' ORDER BY score DESC, id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_recent(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def update_news(news_id: int, *, status: str | None = None, rewritten_text: str | None = None, score: int | None = None):
    fields, values = [], []
    if status is not None:
        fields.append("status=?")
        values.append(status)
        if status == "published":
            fields.append("published_at=CURRENT_TIMESTAMP")
    if rewritten_text is not None:
        fields.append("rewritten_text=?")
        values.append(rewritten_text)
    if score is not None:
        fields.append("score=?")
        values.append(score)
    if not fields:
        return
    values.append(news_id)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(f"UPDATE news SET {', '.join(fields)} WHERE id=?", values)
        await db.commit()


async def stats():
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("""SELECT
            COUNT(*) total,
            SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) ready,
            SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) published,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) rejected,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) skipped,
            ROUND(AVG(score),1) avg_score,
            SUM(CASE WHEN date(created_at)=date('now') THEN 1 ELSE 0 END) today
            FROM news""")
        row = await cur.fetchone()
        return {
            "total": row[0] or 0, "ready": row[1] or 0, "published": row[2] or 0,
            "rejected": row[3] or 0, "skipped": row[4] or 0, "avg_score": row[5] or 0,
            "today": row[6] or 0,
        }


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()
