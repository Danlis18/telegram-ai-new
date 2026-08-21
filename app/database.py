import hashlib
from pathlib import Path

import aiosqlite

from app.config import settings
from app.tenant import get_current_user_id


async def _ensure_column(db, table: str, name: str, definition: str) -> None:
    cur = await db.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in await cur.fetchall()}
    if name not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _ctx_user_id(explicit: int | None = None) -> int | None:
    if explicit is not None:
        return int(explicit)
    current = get_current_user_id()
    return int(current) if current is not None else None


def _scoped_key(key: str, user_id: int) -> str:
    return f"u:{int(user_id)}:{key}"


async def init_db():
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    owner_id = int(settings.admin_user_id) if settings.admin_user_id else None

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
        await _ensure_column(db, "news", "media_type", "TEXT")
        await _ensure_column(db, "news", "media_file_id", "TEXT")
        await _ensure_column(db, "news", "original_media_file_id", "TEXT")
        await _ensure_column(db, "news", "scheduled_at", "DATETIME")
        await _ensure_column(db, "news", "published_at", "DATETIME")
        await _ensure_column(db, "news", "user_id", "INTEGER")

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
        await _ensure_column(db, "editorial_feedback", "user_id", "INTEGER")

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
        await _ensure_column(db, "custom_image_templates", "user_id", "INTEGER")

        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'user',
            display_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, username)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_ref TEXT NOT NULL,
            title TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, channel_ref)
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_news_user_status ON news(user_id,status,id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_sources_name ON user_sources(username,is_active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_targets_user ON user_targets(user_id,is_active,is_default)")

        if owner_id:
            await db.execute(
                """INSERT INTO users (telegram_user_id,role,display_name,is_active)
                   VALUES (?,'owner','Owner',1)
                   ON CONFLICT(telegram_user_id) DO UPDATE SET role='owner', is_active=1""",
                (owner_id,),
            )

            # Preserve all legacy owner data when upgrading to multi-user mode.
            cur = await db.execute("SELECT id,original_text FROM news WHERE user_id IS NULL")
            for row_id, original_text in await cur.fetchall():
                fp = fingerprint(original_text or "", owner_id)
                await db.execute(
                    "UPDATE news SET user_id=?, fingerprint=? WHERE id=?",
                    (owner_id, fp, row_id),
                )
            await db.execute("UPDATE editorial_feedback SET user_id=? WHERE user_id IS NULL", (owner_id,))
            await db.execute("UPDATE custom_image_templates SET user_id=? WHERE user_id IS NULL", (owner_id,))

            user_setting_keys = (
                "text_prompt_custom",
                "emoji_prompt_custom",
                "image_prompt_custom",
                "sports_news_logo_b64",
                "template_mode",
                "selected_template_id",
                "publish_mode",
                "photo_edit_mode",
                "processing_paused",
            )
            for key in user_setting_keys:
                cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
                row = await cur.fetchone()
                if row:
                    await db.execute(
                        "INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)",
                        (_scoped_key(key, owner_id), row[0]),
                    )

            cur = await db.execute("SELECT key,value FROM generation_stats WHERE key NOT LIKE 'u:%'")
            for key, value in await cur.fetchall():
                await db.execute(
                    "INSERT OR IGNORE INTO generation_stats (key,value) VALUES (?,?)",
                    (_scoped_key(str(key), owner_id), int(value)),
                )

        await db.commit()


def fingerprint(text: str, user_id: int | None = None) -> str:
    normalized = " ".join((text or "").lower().split())
    prefix = f"{int(user_id)}:" if user_id is not None else ""
    return hashlib.sha256((prefix + normalized).encode()).hexdigest()


async def seen(text: str, user_id: int | None = None) -> bool:
    uid = _ctx_user_id(user_id) or (int(settings.admin_user_id) if settings.admin_user_id else None)
    fp = fingerprint(text, uid)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT 1 FROM news WHERE fingerprint=? LIMIT 1", (fp,))
        return await cur.fetchone() is not None


async def save(
    source: str,
    message_id: int,
    original: str,
    rewritten: str,
    score: int,
    status: str,
    user_id: int | None = None,
) -> int | None:
    uid = _ctx_user_id(user_id) or (int(settings.admin_user_id) if settings.admin_user_id else None)
    if uid is None:
        raise RuntimeError("Cannot save news without user workspace")
    fp = fingerprint(original, uid)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO news
               (fingerprint,user_id,source,source_message_id,original_text,rewritten_text,score,status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (fp, uid, source, message_id, original, rewritten, score, status),
        )
        await db.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        cur = await db.execute("SELECT id FROM news WHERE fingerprint=? LIMIT 1", (fp,))
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def get_setting(key: str, default: str | None = None) -> str | None:
    uid = _ctx_user_id()
    db_key = _scoped_key(key, uid) if uid is not None else key
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (db_key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    uid = _ctx_user_id()
    db_key = _scoped_key(key, uid) if uid is not None else key
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (db_key, value),
        )
        await db.commit()


async def increment_generation_stat(key: str, amount: int = 1) -> None:
    uid = _ctx_user_id()
    db_key = _scoped_key(key, uid) if uid is not None else key
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO generation_stats (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=value+excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (db_key, int(amount)),
        )
        await db.commit()


async def get_generation_stats() -> dict[str, int]:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        if uid is None:
            cur = await db.execute("SELECT key,value FROM generation_stats WHERE key NOT LIKE 'u:%'")
            return {str(row[0]): int(row[1]) for row in await cur.fetchall()}
        prefix = _scoped_key("", uid)
        cur = await db.execute("SELECT key,value FROM generation_stats WHERE key LIKE ?", (prefix + "%",))
        return {str(row[0])[len(prefix):]: int(row[1]) for row in await cur.fetchall()}


async def save_custom_template(name: str, image_bytes: bytes) -> int:
    uid = _ctx_user_id() or (int(settings.admin_user_id) if settings.admin_user_id else None)
    if uid is None:
        raise RuntimeError("Template workspace is not available")
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "INSERT INTO custom_image_templates (user_id,name,image_blob) VALUES (?,?,?)",
            (uid, name.strip()[:120] or "Template", image_bytes),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_custom_templates(limit: int = 20) -> list[dict]:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute(
                "SELECT id,user_id,name,created_at FROM custom_image_templates ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = await db.execute(
                "SELECT id,user_id,name,created_at FROM custom_image_templates WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (uid, limit),
            )
        return [dict(row) for row in await cur.fetchall()]


async def get_custom_template(template_id: int) -> dict | None:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute(
                "SELECT id,user_id,name,image_blob,created_at FROM custom_image_templates WHERE id=?",
                (template_id,),
            )
        else:
            cur = await db.execute(
                "SELECT id,user_id,name,image_blob,created_at FROM custom_image_templates WHERE id=? AND user_id=?",
                (template_id, uid),
            )
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_custom_template(template_id: int) -> None:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        if uid is None:
            await db.execute("DELETE FROM custom_image_templates WHERE id=?", (template_id,))
        else:
            await db.execute("DELETE FROM custom_image_templates WHERE id=? AND user_id=?", (template_id, uid))
        await db.commit()


async def get_news(news_id: int):
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute("SELECT * FROM news WHERE id=?", (news_id,))
        else:
            cur = await db.execute("SELECT * FROM news WHERE id=? AND user_id=?", (news_id, uid))
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
    uid = _ctx_user_id()
    where = "id=?" + (" AND user_id=?" if uid is not None else "")
    params = [v for _, v in items] + [news_id] + ([uid] if uid is not None else [])
    sql = "UPDATE news SET " + ", ".join(f"{k}=?" for k, _ in items) + " WHERE " + where
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(sql, params)
        await db.commit()


async def save_editorial_feedback(news_id: int, original_text: str, ai_text: str, corrected_text: str) -> None:
    uid = _ctx_user_id() or (int(settings.admin_user_id) if settings.admin_user_id else None)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO editorial_feedback (user_id,news_id,original_text,ai_text,corrected_text) VALUES (?,?,?,?,?)",
            (uid, news_id, original_text, ai_text, corrected_text),
        )
        await db.commit()


async def get_style_examples(limit: int = 6) -> list[dict]:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute(
                "SELECT original_text,ai_text,corrected_text FROM editorial_feedback ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = await db.execute(
                "SELECT original_text,ai_text,corrected_text FROM editorial_feedback WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (uid, limit),
            )
        return [dict(r) for r in await cur.fetchall()]


async def get_feedback_count() -> int:
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        if uid is None:
            cur = await db.execute("SELECT COUNT(*) FROM editorial_feedback")
        else:
            cur = await db.execute("SELECT COUNT(*) FROM editorial_feedback WHERE user_id=?", (uid,))
        return int((await cur.fetchone())[0])


async def _news_list(where: str, params: tuple, limit: int) -> list[dict]:
    uid = _ctx_user_id()
    user_clause = " AND user_id=?" if uid is not None else ""
    final_params = params + ((uid,) if uid is not None else ()) + (limit,)
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM news WHERE {where}{user_clause} ORDER BY id DESC LIMIT ?",
            final_params,
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_queue(limit: int = 20):
    return await _news_list("status='ready'", (), limit)


async def get_scheduled(limit: int = 20):
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute(
                "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL ORDER BY scheduled_at ASC LIMIT ?",
                (limit,),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL AND user_id=? ORDER BY scheduled_at ASC LIMIT ?",
                (uid, limit),
            )
        return [dict(r) for r in await cur.fetchall()]


async def get_due_scheduled(now_utc: str, limit: int = 20):
    # No user context in the background scheduler = intentionally scan all workspaces.
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute(
                "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=? ORDER BY scheduled_at ASC LIMIT ?",
                (now_utc, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM news WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=? AND user_id=? ORDER BY scheduled_at ASC LIMIT ?",
                (now_utc, uid, limit),
            )
        return [dict(r) for r in await cur.fetchall()]


async def get_archive(limit: int = 15):
    return await _news_list(
        "status IN ('published','skipped','rejected','ai_error','raw','advertising')",
        (),
        limit,
    )


async def get_recent(limit: int = 12):
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if uid is None:
            cur = await db.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,))
        else:
            cur = await db.execute("SELECT * FROM news WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))
        return [dict(r) for r in await cur.fetchall()]


async def stats():
    uid = _ctx_user_id()
    async with aiosqlite.connect(settings.database_path) as db:
        result = {}
        user_clause = f" AND user_id={int(uid)}" if uid is not None else ""
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
            cur = await db.execute(f"SELECT COUNT(*) FROM news WHERE {where}{user_clause}")
            result[key] = (await cur.fetchone())[0]
        avg_where = "score > 0" + user_clause
        cur = await db.execute(f"SELECT COALESCE(AVG(score),0) FROM news WHERE {avg_where}")
        result["avg_score"] = round((await cur.fetchone())[0], 1)
        return result


# ---- Multi-user accounts / workspaces -------------------------------------------------


async def add_user(telegram_user_id: int, display_name: str = "", role: str = "user") -> None:
    role = "owner" if role == "owner" else "user"
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO users (telegram_user_id,role,display_name,is_active)
               VALUES (?,?,?,1)
               ON CONFLICT(telegram_user_id) DO UPDATE SET display_name=excluded.display_name, is_active=1""",
            (int(telegram_user_id), role, display_name.strip()[:120]),
        )
        await db.commit()


async def set_user_active(telegram_user_id: int, active: bool) -> None:
    if settings.admin_user_id and int(telegram_user_id) == int(settings.admin_user_id):
        return
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("UPDATE users SET is_active=? WHERE telegram_user_id=?", (1 if active else 0, int(telegram_user_id)))
        await db.commit()


async def get_user(telegram_user_id: int) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE telegram_user_id=?", (int(telegram_user_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_users(active_only: bool = False, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE is_active=1" if active_only else ""
        cur = await db.execute(
            f"SELECT * FROM users {where} ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def is_user_active(telegram_user_id: int) -> bool:
    if settings.admin_user_id and int(telegram_user_id) == int(settings.admin_user_id):
        return True
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE telegram_user_id=? AND is_active=1", (int(telegram_user_id),))
        return await cur.fetchone() is not None


async def seed_owner_workspace(default_sources: list[str], default_target: str | None) -> None:
    if not settings.admin_user_id:
        return
    owner_id = int(settings.admin_user_id)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='owner_workspace_seeded'")
        if await cur.fetchone():
            return
        for source in default_sources:
            username = source.strip().lstrip("@").lower()
            if username:
                await db.execute(
                    "INSERT OR IGNORE INTO user_sources (user_id,username,is_active) VALUES (?,?,1)",
                    (owner_id, username),
                )
        if default_target:
            await db.execute(
                "INSERT OR IGNORE INTO user_targets (user_id,channel_ref,title,is_default,is_active) VALUES (?,?,?,1,1)",
                (owner_id, str(default_target), "SPORTS NEWS"),
            )
            await db.execute(
                "UPDATE user_targets SET is_default=CASE WHEN channel_ref=? THEN 1 ELSE 0 END WHERE user_id=?",
                (str(default_target), owner_id),
            )
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('owner_workspace_seeded','1')")
        await db.commit()


async def add_user_source(user_id: int, username: str) -> None:
    username = username.strip().lstrip("@").lower()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO user_sources (user_id,username,is_active) VALUES (?,?,1)
               ON CONFLICT(user_id,username) DO UPDATE SET is_active=1""",
            (int(user_id), username),
        )
        await db.commit()


async def list_user_sources(user_id: int | None = None, active_only: bool = True) -> list[dict]:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return []
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        active = " AND is_active=1" if active_only else ""
        cur = await db.execute(
            f"SELECT * FROM user_sources WHERE user_id=?{active} ORDER BY username ASC",
            (uid,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_user_source(source_id: int, user_id: int | None = None) -> None:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("DELETE FROM user_sources WHERE id=? AND user_id=?", (int(source_id), uid))
        await db.commit()


async def count_user_sources(user_id: int | None = None) -> int:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return 0
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM user_sources WHERE user_id=? AND is_active=1", (uid,))
        return int((await cur.fetchone())[0])


async def list_all_active_source_usernames() -> list[str]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """SELECT DISTINCT s.username
               FROM user_sources s JOIN users u ON u.telegram_user_id=s.user_id
               WHERE s.is_active=1 AND u.is_active=1
               ORDER BY s.username"""
        )
        return [str(row[0]) for row in await cur.fetchall()]


async def get_source_subscribers(username: str) -> list[int]:
    normalized = username.strip().lstrip("@").lower()
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """SELECT s.user_id
               FROM user_sources s JOIN users u ON u.telegram_user_id=s.user_id
               WHERE lower(s.username)=? AND s.is_active=1 AND u.is_active=1""",
            (normalized,),
        )
        return [int(row[0]) for row in await cur.fetchall()]


async def add_user_target(user_id: int, channel_ref: str, title: str = "") -> int:
    uid = int(user_id)
    ref = str(channel_ref).strip()
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM user_targets WHERE user_id=? AND is_active=1", (uid,))
        make_default = int((await cur.fetchone())[0]) == 0
        await db.execute(
            """INSERT INTO user_targets (user_id,channel_ref,title,is_default,is_active)
               VALUES (?,?,?,?,1)
               ON CONFLICT(user_id,channel_ref) DO UPDATE SET title=excluded.title,is_active=1""",
            (uid, ref, title.strip()[:160], 1 if make_default else 0),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM user_targets WHERE user_id=? AND channel_ref=?", (uid, ref))
        row = await cur.fetchone()
        return int(row[0])


async def list_user_targets(user_id: int | None = None, active_only: bool = True) -> list[dict]:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return []
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        active = " AND is_active=1" if active_only else ""
        cur = await db.execute(
            f"SELECT * FROM user_targets WHERE user_id=?{active} ORDER BY is_default DESC,id ASC",
            (uid,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def set_default_target(target_id: int, user_id: int | None = None) -> None:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT 1 FROM user_targets WHERE id=? AND user_id=? AND is_active=1", (int(target_id), uid))
        if not await cur.fetchone():
            return
        await db.execute("UPDATE user_targets SET is_default=0 WHERE user_id=?", (uid,))
        await db.execute("UPDATE user_targets SET is_default=1 WHERE id=? AND user_id=?", (int(target_id), uid))
        await db.commit()


async def delete_user_target(target_id: int, user_id: int | None = None) -> None:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT is_default FROM user_targets WHERE id=? AND user_id=?", (int(target_id), uid))
        row = await cur.fetchone()
        was_default = bool(row and row[0])
        await db.execute("DELETE FROM user_targets WHERE id=? AND user_id=?", (int(target_id), uid))
        if was_default:
            cur = await db.execute("SELECT id FROM user_targets WHERE user_id=? AND is_active=1 ORDER BY id ASC LIMIT 1", (uid,))
            replacement = await cur.fetchone()
            if replacement:
                await db.execute("UPDATE user_targets SET is_default=1 WHERE id=?", (int(replacement[0]),))
        await db.commit()


async def get_default_target(user_id: int | None = None) -> dict | None:
    uid = _ctx_user_id(user_id)
    if uid is None:
        return None
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM user_targets
               WHERE user_id=? AND is_active=1
               ORDER BY is_default DESC,id ASC LIMIT 1""",
            (uid,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
