import logging
import re
import unicodedata
from contextlib import suppress

import aiosqlite

from app.config import settings

log = logging.getLogger("telegram-ai-news.premium-emoji-registry")

_TAG_RE = re.compile(
    r'<tg-emoji\s+emoji-id=["\'](?P<id>\d+)["\']\s*>(?P<fallback>.*?)</tg-emoji>\s*(?P<label>[^\n]{1,64})?',
    re.IGNORECASE | re.DOTALL,
)
_CLEAN_TAGS = re.compile(r"<[^>]+>")
_STOP_RE = re.compile(r"\s*(?:—|–|\||\s[-:]\s|\d{1,2}:\d{2})\s*")


def normalize_alias(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^a-zа-яіїєґ0-9' ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


async def ensure_emoji_registry_schema() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS premium_emoji_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT NOT NULL,
                alias_norm TEXT NOT NULL UNIQUE,
                emoji_id TEXT NOT NULL,
                fallback TEXT NOT NULL DEFAULT '⚽',
                source TEXT,
                confidence INTEGER NOT NULL DEFAULT 50,
                seen_count INTEGER NOT NULL DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_premium_emoji_alias_norm ON premium_emoji_aliases(alias_norm)")
        await db.commit()


async def remember_alias(alias: str, emoji_id: int | str, fallback: str = "⚽", *, source: str = "learned", confidence: int = 60) -> None:
    alias = (alias or "").strip(" \t\n:—–-|•·")
    alias_norm = normalize_alias(alias)
    if len(alias_norm) < 2 or not str(emoji_id).isdigit():
        return
    if alias_norm in {"sports news", "sport", "football", "soccer", "live", "today", "match"}:
        return
    fallback = (_CLEAN_TAGS.sub("", fallback or "") or "⚽").strip()[:8]
    await ensure_emoji_registry_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO premium_emoji_aliases(alias,alias_norm,emoji_id,fallback,source,confidence,seen_count,updated_at)
               VALUES (?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
               ON CONFLICT(alias_norm) DO UPDATE SET
                 emoji_id=excluded.emoji_id,
                 fallback=excluded.fallback,
                 source=excluded.source,
                 confidence=MAX(premium_emoji_aliases.confidence,excluded.confidence),
                 seen_count=premium_emoji_aliases.seen_count+1,
                 updated_at=CURRENT_TIMESTAMP""",
            (alias[:96], alias_norm[:96], str(emoji_id), fallback, source[:64], max(1, min(100, int(confidence)))),
        )
        await db.commit()


def _label_candidates(raw: str) -> list[str]:
    raw = _CLEAN_TAGS.sub("", raw or "")
    raw = raw.strip(" \t\r\n:—–-|•·")
    if not raw:
        return []
    raw = _STOP_RE.split(raw, maxsplit=1)[0]
    raw = re.sub(r"\([^)]{0,24}\)", "", raw).strip()
    words = raw.split()
    candidates = []
    if 1 <= len(words) <= 6:
        candidates.append(" ".join(words)[:64])
    if len(words) >= 2:
        candidates.append(" ".join(words[:2])[:64])
    if len(words) >= 3:
        candidates.append(" ".join(words[:3])[:64])
    return list(dict.fromkeys(x for x in candidates if len(normalize_alias(x)) >= 2))


async def learn_from_html(text: str, *, source: str = "html") -> int:
    learned = 0
    for match in _TAG_RE.finditer(text or ""):
        emoji_id = match.group("id")
        fallback = _CLEAN_TAGS.sub("", match.group("fallback") or "") or "⚽"
        for alias in _label_candidates(match.group("label") or ""):
            await remember_alias(alias, emoji_id, fallback, source=source, confidence=72)
            learned += 1
    return learned


def _py_index_from_utf16(text: str, units: int) -> int:
    if units <= 0:
        return 0
    used = 0
    for index, char in enumerate(text):
        used += len(char.encode("utf-16-le")) // 2
        if used >= units:
            return index + 1
    return len(text)


async def learn_from_telethon_message(message, *, source: str = "telegram") -> int:
    text = str(getattr(message, "raw_text", None) or getattr(message, "message", None) or "")
    entities = list(getattr(message, "entities", None) or [])
    learned = 0
    for entity in entities:
        emoji_id = getattr(entity, "document_id", None)
        if not emoji_id or "customemoji" not in type(entity).__name__.lower():
            continue
        start_u = int(getattr(entity, "offset", 0) or 0)
        length_u = int(getattr(entity, "length", 0) or 0)
        start = _py_index_from_utf16(text, start_u)
        end = _py_index_from_utf16(text, start_u + length_u)
        fallback = text[start:end].strip() or "⚽"
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = min(len(text), end + 64)
        after = text[end:line_end]
        for alias in _label_candidates(after):
            await remember_alias(alias, emoji_id, fallback, source=source, confidence=86)
            learned += 1
    return learned


async def learn_from_existing_content(limit: int = 300) -> int:
    await ensure_emoji_registry_schema()
    values: list[str] = []
    async with aiosqlite.connect(settings.database_path) as db:
        with suppress(Exception):
            cur = await db.execute("SELECT rewritten_text FROM news WHERE rewritten_text IS NOT NULL ORDER BY id DESC LIMIT ?", (int(limit),))
            values.extend((row[0] or "") for row in await cur.fetchall())
        with suppress(Exception):
            cur = await db.execute("SELECT corrected_text FROM editorial_feedback ORDER BY id DESC LIMIT ?", (max(20, int(limit // 2)),))
            values.extend((row[0] or "") for row in await cur.fetchall())
    learned = 0
    for value in values:
        learned += await learn_from_html(value, source="history")
    if learned:
        log.info("Premium emoji registry learned %d alias observations from history", learned)
    return learned


async def resolve_emoji(aliases: list[str] | tuple[str, ...], fallback: str = "⚽") -> str:
    await ensure_emoji_registry_schema()
    norms = [normalize_alias(alias) for alias in aliases if normalize_alias(alias)]
    if not norms:
        return fallback
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        for norm in norms:
            cur = await db.execute(
                "SELECT emoji_id,fallback FROM premium_emoji_aliases WHERE alias_norm=? ORDER BY confidence DESC,seen_count DESC LIMIT 1",
                (norm,),
            )
            row = await cur.fetchone()
            if row:
                fb = (row["fallback"] or fallback or "⚽").strip()[:8] or fallback
                return f'<tg-emoji emoji-id="{row["emoji_id"]}">{fb}</tg-emoji>'
    return fallback


def install_telegram_emoji_learning(app_main) -> None:
    if getattr(app_main, "_premium_emoji_learning_installed", False):
        return
    original = app_main.process_message

    async def wrapped(event, source: str, user_id: int, **kwargs):
        try:
            message = getattr(event, "message", event)
            await learn_from_telethon_message(message, source=f"telegram:{source}")
        except Exception:
            log.exception("Could not learn Premium emoji aliases from @%s", source)
        return await original(event, source, user_id, **kwargs)

    app_main.process_message = wrapped
    app_main._premium_emoji_learning_installed = True
    log.info("Installed non-invasive Premium emoji alias learning")
