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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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


async def get_news(news_id: int):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM news WHERE id=?", (news_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_news(news_id: int, **fields) -> None:
    allowed = {"rewritten_text", "score", "status"}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    sql = "UPDATE news SET " + ", ".join(f"{k}=?" for k, _ in items) + " WHERE id=?"
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(sql, [v for _, v in items] + [news_id])
        await db.commit()


async def get_queue(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status='ready' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_archive(limit: int = 20):
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news WHERE status IN ('published','skipped','rejected','ai_error','raw') ORDER BY id DESC LIMIT ?",
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
            "published": "status='published'",
            "rejected": "status='rejected'",
            "skipped": "status='skipped'",
            "received": "status='received'",
            "ai_error": "status='ai_error'",
            "raw": "status='raw'",
        }.items():
            cur = await db.execute(f"SELECT COUNT(*) FROM news WHERE {where}")
            result[key] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(AVG(score),0) FROM news WHERE score > 0")
        result["avg_score"] = round((await cur.fetchone())[0], 1)
        return result
