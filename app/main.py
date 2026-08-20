import asyncio
import base64
import html
import logging
import os
from pathlib import Path

from telethon import TelegramClient as TelethonClient, events
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.utils import get_peer_id
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from app.admin_bot import start_admin_bot, stop_admin_bot
from app.ai_editor import rewrite_news
from app.config import settings
from app.database import get_setting, init_db, save, seen, set_setting
from app.sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telegram-ai-news")

ACTIVE_SOURCE_IDS: dict[int, str] = {}


def get_session_file_b64() -> str | None:
    if settings.telegram_session_file_b64:
        return settings.telegram_session_file_b64

    chunks = []
    index = 1
    while True:
        value = os.getenv(f"TELEGRAM_SESSION_FILE_B64_{index}")
        if not value:
            break
        chunks.append(value.strip())
        index += 1

    return "".join(chunks) if chunks else None


def build_reader():
    session_b64 = get_session_file_b64()
    if session_b64:
        from opentele2.tl import TelegramClient as DesktopTelegramClient

        session_path = Path(settings.session_file_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_bytes(base64.b64decode(session_b64))
        return DesktopTelegramClient(str(session_path))

    if settings.telegram_session and settings.telegram_api_id and settings.telegram_api_hash:
        return TelethonClient(
            StringSession(settings.telegram_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )

    raise RuntimeError(
        "Telegram reader is not configured. Set TELEGRAM_SESSION_FILE_B64, numbered TELEGRAM_SESSION_FILE_B64_1..N chunks, "
        "or TELEGRAM_SESSION + TELEGRAM_API_ID + TELEGRAM_API_HASH."
    )


reader = build_reader()
publisher = Bot(settings.telegram_bot_token)


async def notify_admin(text: str, reply_markup=None) -> None:
    if not settings.admin_user_id:
        return
    try:
        await publisher.send_message(
            settings.admin_user_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Failed to send admin notification")


async def sync_sources() -> tuple[int, list[str]]:
    """Resolve monitored channels and join public channels that are not yet in this account."""
    ACTIVE_SOURCE_IDS.clear()
    joined_by_username: dict[str, tuple[int, str]] = {}

    async for dialog in reader.iter_dialogs():
        entity = dialog.entity
        username = getattr(entity, "username", None)
        if username:
            joined_by_username[username.lower()] = (dialog.id, username)

    missing: list[str] = []
    joined_now = 0

    for source in SOURCES:
        cached = joined_by_username.get(source.lower())
        if cached:
            chat_id, canonical = cached
            ACTIVE_SOURCE_IDS[int(chat_id)] = canonical
            continue

        try:
            entity = await reader.get_entity(source)
            try:
                await reader(JoinChannelRequest(entity))
                joined_now += 1
                log.info("Joined source @%s", source)
                await asyncio.sleep(4)
            except UserAlreadyParticipantError:
                pass
            except FloodWaitError as exc:
                log.warning("Telegram FloodWait while joining @%s: %ss", source, exc.seconds)
                missing.append(source)
                # Stop automatic joins for this boot; avoid stressing the Telegram account.
                break
            except Exception:
                log.exception("Could not join source @%s", source)
                missing.append(source)
                continue

            canonical = getattr(entity, "username", None) or source
            ACTIVE_SOURCE_IDS[get_peer_id(entity)] = canonical
        except Exception:
            log.exception("Could not resolve source @%s", source)
            missing.append(source)

    # If joining stopped because of FloodWait, classify remaining unresolved sources as missing.
    resolved_names = {name.lower() for name in ACTIVE_SOURCE_IDS.values()}
    for source in SOURCES:
        if source.lower() not in resolved_names and source not in missing:
            missing.append(source)

    await set_setting("active_sources", str(len(ACTIVE_SOURCE_IDS)))
    await set_setting("missing_sources", ",".join(missing))
    log.info(
        "Source audit complete: active=%d/%d, joined_now=%d, missing=%d",
        len(ACTIVE_SOURCE_IDS), len(SOURCES), joined_now, len(missing),
    )
    return joined_now, missing


@reader.on(events.NewMessage)
async def on_news(event):
    source = ACTIVE_SOURCE_IDS.get(event.chat_id)
    if not source:
        return

    if (await get_setting("processing_paused", "false")) == "true":
        return

    text = (event.raw_text or "").strip()
    if len(text) < 25:
        log.info("Skipped @%s #%s: no/short caption", source, event.id)
        return
    if await seen(text):
        log.info("Skipped @%s #%s: exact duplicate", source, event.id)
        return

    try:
        result = await rewrite_news(text, source)
        score = int(result.get("score", 0))
        rewritten = (result.get("text") or "").strip()
        publishable = bool(result.get("publish")) and score >= settings.min_publish_score and bool(rewritten)
        status = "ready" if publishable else "rejected"

        if publishable and settings.auto_publish:
            await publisher.send_message(settings.target_channel, rewritten, disable_web_page_preview=True)
            status = "published"

        news_id = await save(source, event.id, text, rewritten, score, status)
        log.info("@%s #%s score=%s status=%s news_id=%s", source, event.id, score, status, news_id)

        if status == "ready" and news_id:
            preview = html.escape(rewritten[:2200])
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{news_id}"),
                    InlineKeyboardButton("🔄 Перегенерувати", callback_data=f"regen:{news_id}"),
                ],
                [
                    InlineKeyboardButton("👁 Оригінал", callback_data=f"original:{news_id}"),
                    InlineKeyboardButton("⏭ Пропустити", callback_data=f"skip:{news_id}"),
                ],
            ])
            await notify_admin(
                f"🆕 <b>Нова новина в черзі #{news_id}</b>\n"
                f"📡 @{html.escape(source)} · 🧠 <b>{score}%</b>\n\n{preview}",
                buttons,
            )
    except Exception:
        log.exception("Failed processing @%s #%s", source, event.id)


async def main():
    await init_db()
    admin_app = await start_admin_bot()
    try:
        await reader.start()
        me = await reader.get_me()
        joined_now, missing = await sync_sources()
        log.info(
            "Reader authorized as %s; active sources=%d/%d; auto_publish=%s; admin_bot=online",
            me.id,
            len(ACTIVE_SOURCE_IDS),
            len(SOURCES),
            settings.auto_publish,
        )
        await notify_admin(
            "🟢 <b>AI NEWS CONTROL запущено</b>\n\n"
            f"Reader: <b>ONLINE</b>\n"
            f"Активні джерела: <b>{len(ACTIVE_SOURCE_IDS)}/{len(SOURCES)}</b>\n"
            f"Нових підписок цього запуску: <b>{joined_now}</b>\n"
            f"Недоступних джерел: <b>{len(missing)}</b>\n"
            f"Автопублікація: <b>{'ON' if settings.auto_publish else 'OFF'}</b>\n\n"
            "Коли AI відбере новину, вона автоматично прийде сюди з кнопками керування."
        )
        await reader.run_until_disconnected()
    finally:
        await stop_admin_bot(admin_app)


if __name__ == "__main__":
    asyncio.run(main())
