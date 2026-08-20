import asyncio
import base64
import html
import logging
import os
from datetime import datetime, timedelta, timezone
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
from app.database import get_setting, init_db, save, seen, set_setting, update_news
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
        return TelethonClient(StringSession(settings.telegram_session), settings.telegram_api_id, settings.telegram_api_hash)
    raise RuntimeError("Telegram reader is not configured")


reader = build_reader()
publisher = Bot(settings.telegram_bot_token)


async def notify_admin(text: str, reply_markup=None) -> None:
    if not settings.admin_user_id:
        log.warning("ADMIN_USER_ID is not configured; admin notification skipped")
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

    resolved_names = {name.lower() for name in ACTIVE_SOURCE_IDS.values()}
    for source in SOURCES:
        if source.lower() not in resolved_names and source not in missing:
            missing.append(source)

    await set_setting("active_sources", str(len(ACTIVE_SOURCE_IDS)))
    await set_setting("missing_sources", ",".join(missing))
    log.info("Source audit complete: active=%d/%d, joined_now=%d, missing=%d", len(ACTIVE_SOURCE_IDS), len(SOURCES), joined_now, len(missing))
    return joined_now, missing


def action_buttons(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{news_id}"),
            InlineKeyboardButton("🔄 Перегенерувати", callback_data=f"regen:{news_id}"),
        ],
        [
            InlineKeyboardButton("👁 Оригінал", callback_data=f"original:{news_id}"),
            InlineKeyboardButton("⏭ Пропустити", callback_data=f"skip:{news_id}"),
        ],
    ])


async def process_message(event, source: str, *, backfill: bool = False) -> None:
    if (await get_setting("processing_paused", "false")) == "true":
        return

    text = (event.raw_text or "").strip()
    has_media = bool(getattr(event, "media", None))
    stored_original = text or f"[MEDIA_ONLY] @{source} #{event.id}"

    if await seen(stored_original):
        log.info("Skipped @%s #%s: exact duplicate", source, event.id)
        return

    # Save and show EVERY captured post before AI touches it.
    initial_status = "raw" if len(text) < 25 else "received"
    news_id = await save(source, event.id, stored_original, "", 0, initial_status)
    if not news_id:
        return

    readable = text if text else "📎 Медіапост без текстового підпису"
    media_note = "\n🖼 Є медіа" if has_media else ""
    backfill_note = "\n🕘 Відновлено після перезапуску" if backfill else ""
    await notify_admin(
        f"📥 <b>Отримано пост #{news_id}</b>\n"
        f"📡 @{html.escape(source)}{media_note}{backfill_note}\n\n"
        f"{html.escape(readable[:2500])}\n\n"
        "⏳ <i>AI-обробка запускається окремо; оригінал уже не загубиться.</i>",
        action_buttons(news_id),
    )
    log.info("Captured @%s #%s news_id=%s chars=%d media=%s backfill=%s", source, event.id, news_id, len(text), has_media, backfill)

    # Tiny captions and media-only posts are still visible to admin, but are not sent to AI.
    if len(text) < 25:
        return

    try:
        result = await rewrite_news(text, source)
        score = int(result.get("score", 0))
        rewritten = (result.get("text") or "").strip()
        reason = (result.get("reason") or "").strip()
        publishable = bool(result.get("publish")) and score >= settings.min_publish_score and bool(rewritten)
        status = "ready" if publishable else "rejected"

        if publishable and settings.auto_publish:
            await publisher.send_message(settings.target_channel, rewritten, disable_web_page_preview=True)
            status = "published"

        await update_news(news_id, rewritten_text=rewritten, score=score, status=status)
        log.info("Processed @%s #%s score=%s status=%s news_id=%s reason=%s", source, event.id, score, status, news_id, reason[:200])

        if status != "published":
            preview = html.escape((rewritten or text)[:2400])
            badge = "✅" if status == "ready" else "⚠️"
            label = "AI-переписав і схвалив" if status == "ready" else "AI обробив, але не схвалив автоматично"
            reason_html = f"\n📝 Причина: {html.escape(reason[:500])}" if reason else ""
            await notify_admin(
                f"{badge} <b>{label} #{news_id}</b>\n"
                f"📡 @{html.escape(source)} · 🧠 <b>{score}%</b>{reason_html}\n\n{preview}",
                action_buttons(news_id),
            )
    except Exception as exc:
        await update_news(news_id, status="ai_error")
        log.exception("AI failed for @%s #%s news_id=%s", source, event.id, news_id)
        await notify_admin(
            f"🔴 <b>AI ERROR #{news_id}</b>\n"
            f"📡 @{html.escape(source)}\n\n"
            f"Оригінальний пост збережено і доступний нижче.\n"
            f"Помилка: <code>{html.escape(type(exc).__name__)}</code>\n\n"
            f"{html.escape(text[:2400])}",
            action_buttons(news_id),
        )


@reader.on(events.NewMessage)
async def on_news(event):
    source = ACTIVE_SOURCE_IDS.get(event.chat_id)
    if not source:
        return
    await process_message(event, source)


async def backfill_recent(minutes: int = 90, limit_per_source: int = 3) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    processed = 0
    for chat_id, source in list(ACTIVE_SOURCE_IDS.items()):
        try:
            messages = await reader.get_messages(chat_id, limit=limit_per_source)
            for message in reversed(messages):
                if not message.date or message.date < cutoff:
                    continue
                stored_original = (message.raw_text or "").strip() or f"[MEDIA_ONLY] @{source} #{message.id}"
                if await seen(stored_original):
                    continue
                await process_message(message, source, backfill=True)
                processed += 1
            await asyncio.sleep(0.12)
        except FloodWaitError as exc:
            log.warning("FloodWait during backfill @%s: %ss", source, exc.seconds)
            break
        except Exception:
            log.exception("Backfill failed for @%s", source)
    log.info("Backfill complete: processed_candidates=%d", processed)
    return processed


async def main():
    await init_db()
    admin_app = await start_admin_bot()
    try:
        await reader.start()
        me = await reader.get_me()
        joined_now, missing = await sync_sources()
        log.info("Reader authorized as %s; active sources=%d/%d; auto_publish=%s; admin_bot=online", me.id, len(ACTIVE_SOURCE_IDS), len(SOURCES), settings.auto_publish)
        await notify_admin(
            "🟢 <b>AI NEWS CONTROL запущено</b>\n\n"
            f"Reader: <b>ONLINE</b>\n"
            f"Активні джерела: <b>{len(ACTIVE_SOURCE_IDS)}/{len(SOURCES)}</b>\n"
            f"Нових підписок цього запуску: <b>{joined_now}</b>\n"
            f"Недоступних джерел: <b>{len(missing)}</b>\n"
            f"Автопублікація: <b>{'ON' if settings.auto_publish else 'OFF'}</b>\n\n"
            "Тепер КОЖЕН отриманий пост спочатку приходить у бот як оригінал, а потім окремо — результат AI."
        )
        asyncio.create_task(backfill_recent())
        await reader.run_until_disconnected()
    finally:
        await stop_admin_bot(admin_app)


if __name__ == "__main__":
    asyncio.run(main())
