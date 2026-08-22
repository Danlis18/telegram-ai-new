import html
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from openai import AsyncOpenAI

from app.config import settings
from app.database import get_setting, set_setting
from app.tenant import get_current_user_id

client = AsyncOpenAI(api_key=settings.openai_api_key)

PERIODS = {
    "morning": (6, 12, "🌅 Ранок 06:00–12:00", 1),
    "day": (12, 18, "☀️ День 12:00–18:00", 2),
    "evening": (18, 24, "🌙 Вечір 18:00–00:00", 3),
}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.publish_timezone)
    except Exception:
        return ZoneInfo("Europe/Kyiv")


async def ensure_policy_schema() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS rejection_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            news_id INTEGER,
            source TEXT,
            original_text TEXT,
            rewritten_text TEXT,
            reason TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS replacement_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            rejected_news_id INTEGER,
            period_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            fulfilled_at DATETIME
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rejection_feedback_user ON rejection_feedback(user_id,id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_replacement_pending ON replacement_requests(status,user_id,id)")
        await db.commit()


async def get_period_quota(period_key: str) -> int:
    if period_key not in PERIODS:
        return 0
    default = PERIODS[period_key][3]
    raw = await get_setting(f"quota_{period_key}", str(default))
    try:
        return max(0, min(20, int(raw or default)))
    except Exception:
        return default


async def set_period_quota(period_key: str, value: int) -> int:
    if period_key not in PERIODS:
        raise ValueError("Unknown period")
    value = max(0, min(20, int(value)))
    await set_setting(f"quota_{period_key}", str(value))
    return value


def current_period(now_local: datetime | None = None) -> str | None:
    now_local = now_local or datetime.now(_tz())
    hour = now_local.hour
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "day"
    if 18 <= hour < 24:
        return "evening"
    return None


def _period_utc_bounds(period_key: str, now_local: datetime | None = None) -> tuple[str, str]:
    now_local = now_local or datetime.now(_tz())
    start_h, end_h, _, _ = PERIODS[period_key]
    day = now_local.date()
    start_local = datetime.combine(day, time(hour=start_h), tzinfo=_tz())
    if end_h == 24:
        end_local = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=_tz())
    else:
        end_local = datetime.combine(day, time(hour=end_h), tzinfo=_tz())
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc


async def count_active_posts_in_period(user_id: int, period_key: str) -> int:
    start_utc, end_utc = _period_utc_bounds(period_key)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """SELECT COUNT(*) FROM news
               WHERE user_id=? AND created_at>=? AND created_at<?
                 AND status IN ('ready','scheduled','published')""",
            (int(user_id), start_utc, end_utc),
        )
        return int((await cur.fetchone())[0])


async def quota_state(user_id: int) -> dict:
    result = {}
    for key, (_, _, label, _) in PERIODS.items():
        result[key] = {
            "label": label,
            "quota": await get_period_quota(key),
            "count": await count_active_posts_in_period(user_id, key),
        }
    return result


async def can_accept_candidate(user_id: int) -> tuple[bool, str | None, int, int]:
    period_key = current_period()
    if not period_key:
        return False, None, 0, 0
    quota = await get_period_quota(period_key)
    count = await count_active_posts_in_period(user_id, period_key)
    return count < quota, period_key, count, quota


async def save_rejection_feedback(row: dict, reason: str, user_id: int) -> None:
    await ensure_policy_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO rejection_feedback
               (user_id,news_id,source,original_text,rewritten_text,reason)
               VALUES (?,?,?,?,?,?)""",
            (
                int(user_id),
                row.get("id"),
                row.get("source"),
                row.get("original_text") or "",
                row.get("rewritten_text") or "",
                reason.strip()[:1200],
            ),
        )
        await db.commit()


async def get_rejection_examples(user_id: int, limit: int = 8) -> list[dict]:
    await ensure_policy_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT source,original_text,rewritten_text,reason,created_at
               FROM rejection_feedback WHERE user_id=? ORDER BY id DESC LIMIT ?""",
            (int(user_id), int(limit)),
        )
        return [dict(r) for r in await cur.fetchall()]


async def candidate_matches_preferences(text: str, source: str, user_id: int) -> tuple[bool, str]:
    examples = await get_rejection_examples(user_id, 8)
    if not examples:
        return True, "no rejection history"

    history_parts = []
    for idx, item in enumerate(reversed(examples), 1):
        rejected = (item.get("rewritten_text") or item.get("original_text") or "")[:500]
        reason = (item.get("reason") or "")[:300]
        history_parts.append(f"{idx}. Відхилено: {rejected}\nПричина: {reason}")
    history = "\n\n".join(history_parts)

    try:
        response = await client.responses.create(
            model=settings.openai_model,
            input=[{
                "role": "user",
                "content": (
                    "Ти фільтр спортивних новин для конкретного редактора. "
                    "На основі його попередніх ручних відхилень визнач, чи варто показувати нову новину. "
                    "Не узагальнюй надто агресивно: відхиляй тільки коли нова новина явно повторює небажаний тип, "
                    "неактуальність, слабку цінність або іншу чітку причину з історії. "
                    "Відповідь рівно у форматі KEEP|коротка причина або REJECT|коротка причина.\n\n"
                    f"ІСТОРІЯ ВІДХИЛЕНЬ:\n{history}\n\n"
                    f"НОВА НОВИНА з @{source}:\n{text[:2200]}"
                ),
            }],
        )
        verdict = (response.output_text or "").strip()
        if verdict.upper().startswith("REJECT|"):
            return False, verdict.split("|", 1)[1][:300]
        return True, verdict.split("|", 1)[-1][:300] if "|" in verdict else verdict[:300]
    except Exception as exc:
        return True, f"preference filter fallback: {type(exc).__name__}"


async def create_replacement_request(user_id: int, rejected_news_id: int, period_key: str | None = None) -> int:
    await ensure_policy_schema()
    period_key = period_key or current_period() or "morning"
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """INSERT INTO replacement_requests (user_id,rejected_news_id,period_key,status)
               VALUES (?,?,?,'pending')""",
            (int(user_id), int(rejected_news_id), period_key),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_pending_replacements(limit: int = 20) -> list[dict]:
    await ensure_policy_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM replacement_requests WHERE status='pending' ORDER BY id ASC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in await cur.fetchall()]


async def finish_replacement_request(request_id: int, status: str = "fulfilled") -> None:
    await ensure_policy_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE replacement_requests SET status=?,fulfilled_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, int(request_id)),
        )
        await db.commit()


def rejection_reason_label(code: str) -> str:
    return {
        "stale": "Неактуальна / запізніла новина",
        "weak": "Слабка / нецікава новина",
        "duplicate": "Повтор або вже було",
        "topic": "Не підходить тематика",
        "bad_source": "Не довіряю / слабке джерело",
    }.get(code, code)
