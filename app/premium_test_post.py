import logging

import aiosqlite

from app.config import settings
from app.user_publisher import (
    _ready_client,
    _resolve_target,
    get_user_publisher_status,
    initialize_user_publisher,
    telegram_html_to_mtproto,
)

log = logging.getLogger("telegram-ai-news.premium-test-post")

_FLAG = "premium_direct_test_v1_done"
_TEST_HTML = (
    '<tg-emoji emoji-id="5411580731929411768">🚀</tg-emoji> '
    '<b>Тест Premium emoji</b>\n\n'
    'Якщо цей 🚀 відображається як Premium emoji — публікація через Premium-акаунт працює.'
)


async def _is_done() -> bool:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS runtime_flags (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        cur = await db.execute("SELECT value FROM runtime_flags WHERE key=?", (_FLAG,))
        row = await cur.fetchone()
        await db.commit()
        return bool(row and row[0] == "1")


async def _mark_done() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS runtime_flags (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT OR REPLACE INTO runtime_flags(key,value) VALUES(?,?)",
            (_FLAG, "1"),
        )
        await db.commit()


async def run_one_time_premium_test(app_main) -> bool:
    """Publish exactly one direct MTProto test post, guarded by persistent SQLite state."""
    if await _is_done():
        log.info("One-time Premium publisher test already completed; skipping")
        return True

    state = await initialize_user_publisher()
    if not state.get("configured"):
        log.warning("Premium publisher test skipped: publisher session is not configured")
        return False
    if not state.get("online"):
        error = state.get("error") or "publisher offline"
        log.error("Premium publisher test skipped: %s", error)
        if settings.admin_user_id:
            await app_main.notify_user(
                int(settings.admin_user_id),
                f"🔴 <b>Premium test не відправлено</b>\n\n<code>{error}</code>",
            )
        return False

    try:
        client = await _ready_client()
        entity = await _resolve_target(client, settings.target_channel)
        text, entities = telegram_html_to_mtproto(_TEST_HTML)
        await client.send_message(
            entity,
            text,
            formatting_entities=entities,
            link_preview=False,
        )
        await _mark_done()
        log.info(
            "One-time Premium emoji test published to %s via @%s",
            settings.target_channel,
            get_user_publisher_status().get("username") or "publisher",
        )
        if settings.admin_user_id:
            await app_main.notify_user(
                int(settings.admin_user_id),
                "✅ <b>Premium emoji test опубліковано напряму в канал</b>\n\n"
                "Відправник: Premium Telegram account через MTProto.\n"
                "Тест одноразовий і повторно після рестартів не відправлятиметься.",
            )
        return True
    except Exception as exc:
        log.exception("One-time Premium emoji test failed")
        if settings.admin_user_id:
            await app_main.notify_user(
                int(settings.admin_user_id),
                "🔴 <b>Premium emoji test не вдався</b>\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:700]}</code>",
            )
        return False
