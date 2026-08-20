import asyncio
import base64
import logging
import os
from pathlib import Path

from telethon import TelegramClient as TelethonClient, events
from telethon.sessions import StringSession
from telegram import Bot

from app.admin_bot import start_admin_bot, stop_admin_bot
from app.ai_editor import rewrite_news
from app.config import settings
from app.database import get_setting, init_db, save, seen
from app.sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telegram-ai-news")


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


@reader.on(events.NewMessage(chats=SOURCES))
async def on_news(event):
    if (await get_setting("processing_paused", "false")) == "true":
        return

    text = (event.raw_text or "").strip()
    if len(text) < 25 or await seen(text):
        return

    chat = await event.get_chat()
    source = getattr(chat, "username", None) or str(event.chat_id)
    try:
        result = await rewrite_news(text, source)
        score = int(result.get("score", 0))
        rewritten = result.get("text", "").strip()
        publish = bool(result.get("publish")) and score >= settings.min_publish_score and rewritten
        status = "ready"

        if publish and settings.auto_publish:
            await publisher.send_message(settings.target_channel, rewritten, disable_web_page_preview=True)
            status = "published"
        elif not publish:
            status = "rejected"

        await save(source, event.id, text, rewritten, score, status)
        log.info("@%s #%s score=%s status=%s", source, event.id, score, status)
    except Exception:
        log.exception("Failed processing @%s #%s", source, event.id)


async def main():
    await init_db()
    admin_app = await start_admin_bot()
    try:
        await reader.start()
        me = await reader.get_me()
        log.info(
            "Reader authorized as %s; watching %d sources; auto_publish=%s; admin_bot=online",
            me.id,
            len(SOURCES),
            settings.auto_publish,
        )
        await reader.run_until_disconnected()
    finally:
        await stop_admin_bot(admin_app)


if __name__ == "__main__":
    asyncio.run(main())
