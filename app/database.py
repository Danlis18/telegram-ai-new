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
