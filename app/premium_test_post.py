import logging

import aiosqlite

from app.config import settings
from app.database import get_default_target
from app.user_publisher import (
    _ready_client,
    _resolve_target,
    get_user_publisher_status,
    initialize_user_publisher,
    publisher_configured,
    telegram_html_to_mtproto,
)

log = logging.getLogger("telegram-ai-news.premium-test-post")

# v2 intentionally forces one fresh test after the first diagnostic attempt.
# The flag is written only after Telegram confirms send_message completed.
_FLAG = "premium_direct_test_v2_done"
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


async def _notify(app_main, text: str) -> None:
    if not settings.admin_user_id:
        log.warning("Cannot send Premium test diagnostic: ADMIN_USER_ID is not configured")
        return
    try:
        await app_main.notify_user(int(settings.admin_user_id), text)
    except Exception:
        log.exception("Could not send Premium test diagnostic to owner")


async def _publication_target() -> str:
    """Prefer the workspace target actually used by the bot over legacy env TARGET_CHANNEL."""
    if settings.admin_user_id:
        try:
            target = await get_default_target(int(settings.admin_user_id))
            if target and target.get("channel_ref"):
                return str(target["channel_ref"])
        except Exception:
            log.exception("Could not resolve default publication target for Premium test")
    return str(settings.target_channel)


async def run_one_time_premium_test(app_main) -> bool:
    """Publish one direct MTProto test and always report why it did/did not run."""
    if await _is_done():
        log.info("One-time Premium publisher v2 test already completed; skipping")
        return True

    # Do not fail silently when Railway variables were named incorrectly or not saved.
    if not publisher_configured():
        log.error("Premium publisher test cannot start: publisher session env is not configured")
        await _notify(
            app_main,
            "🔴 <b>Premium test не запущено</b>\n\n"
            "Railway не бачить сесію Premium-публікатора. Перевір наявність саме цих Variables:\n"
            "<code>TELEGRAM_PUBLISHER_SESSION_FILE_B64_1</code>\n"
            "<code>TELEGRAM_PUBLISHER_SESSION_FILE_B64_2</code>\n\n"
            "Після збереження Variables зроби redeploy.",
        )
        return False

    state = await initialize_user_publisher()
    if not state.get("online"):
        error = state.get("error") or "publisher offline"
        log.error("Premium publisher test skipped: %s", error)
        await _notify(
            app_main,
            "🔴 <b>Premium test не відправлено</b>\n\n"
            f"Publisher session знайдена, але акаунт не готовий:\n<code>{error}</code>",
        )
        return False

    destination = await _publication_target()
    username = get_user_publisher_status().get("username") or "publisher"
    try:
        client = await _ready_client()
        entity = await _resolve_target(client, destination)
        text, entities = telegram_html_to_mtproto(_TEST_HTML)
        sent = await client.send_message(
            entity,
            text,
            formatting_entities=entities,
            link_preview=False,
        )
        if not sent or not getattr(sent, "id", None):
            raise RuntimeError("Telegram did not return a message id after send_message")

        await _mark_done()
        log.info(
            "One-time Premium emoji v2 test published destination=%s message_id=%s via @%s",
            destination,
            sent.id,
            username,
        )
        await _notify(
            app_main,
            "✅ <b>Premium emoji test опубліковано</b>\n\n"
            f"Publisher: <code>@{username}</code>\n"
            f"Канал: <code>{destination}</code>\n"
            f"Message ID: <code>{sent.id}</code>\n\n"
            "Перевір у каналі, чи 🚀 відображається саме як Premium/custom emoji.",
        )
        return True
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:700]}"
        log.exception("One-time Premium emoji v2 test failed")
        await _notify(
            app_main,
            "🔴 <b>Premium emoji test не вдався</b>\n\n"
            f"Publisher: <code>@{username}</code>\n"
            f"Канал: <code>{destination}</code>\n"
            f"Помилка: <code>{error}</code>\n\n"
            "Тест НЕ позначено виконаним — після виправлення він спробує ще раз.",
        )
        return False
