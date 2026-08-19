import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Bot

from app.ai_editor import rewrite_news
from app.config import settings
from app.database import init_db, save, seen
from app.sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telegram-ai-news")

reader = TelegramClient(StringSession(settings.telegram_session), settings.telegram_api_id, settings.telegram_api_hash)
publisher = Bot(settings.telegram_bot_token)


@reader.on(events.NewMessage(chats=SOURCES))
async def on_news(event):
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
    await reader.start()
    me = await reader.get_me()
    log.info("Reader authorized as %s; watching %d sources; auto_publish=%s", me.id, len(SOURCES), settings.auto_publish)
    await reader.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
