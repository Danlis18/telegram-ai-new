import logging
from datetime import datetime, timezone
from io import BytesIO

from app.ai_editor import generate_news_image
from app.config import settings
from app.database import get_news, get_setting, update_news
from app.formatting import post_html

log = logging.getLogger("telegram-ai-news.publishing")


def utc_now_db() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def get_publish_mode() -> str:
    default = "auto" if settings.auto_publish else "manual"
    value = (await get_setting("publish_mode", default) or default).lower()
    return value if value in {"auto", "manual"} else default


async def get_photo_edit_mode() -> str:
    value = (await get_setting("photo_edit_mode", "manual") or "manual").lower()
    return value if value in {"auto", "manual"} else "manual"


async def publish_row(bot, row: dict) -> None:
    text = post_html(row.get("rewritten_text") or "")
    media_type = row.get("media_type")
    file_id = row.get("media_file_id")

    if media_type == "photo" and file_id:
        await bot.send_photo(settings.target_channel, photo=file_id, caption=text, parse_mode="HTML")
    elif media_type == "video" and file_id:
        await bot.send_video(
            settings.target_channel,
            video=file_id,
            caption=text,
            parse_mode="HTML",
            supports_streaming=True,
        )
    else:
        await bot.send_message(
            settings.target_channel,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def auto_edit_photo(bot, row: dict) -> dict:
    if row.get("media_type") != "photo":
        return row

    file_id = row.get("original_media_file_id") or row.get("media_file_id")
    if not file_id:
        raise RuntimeError("AUTO_PHOTO_EDIT: original photo file_id is missing")

    tg_file = await bot.get_file(file_id)
    source = bytes(await tg_file.download_as_bytearray())
    if not source:
        raise RuntimeError("AUTO_PHOTO_EDIT: Telegram returned empty photo")

    edited = await generate_news_image(
        row.get("rewritten_text") or row.get("original_text") or "",
        source_image=source,
    )

    upload = BytesIO(edited)
    upload.name = f"sports_news_auto_{row['id']}.jpg"
    sent = await bot.send_photo(
        settings.admin_user_id,
        photo=upload,
        caption=f"🤖 <b>Фото #{row['id']} оброблено автоматично</b>",
        parse_mode="HTML",
    )
    edited_file_id = sent.photo[-1].file_id
    await update_news(row["id"], media_file_id=edited_file_id)
    updated = await get_news(row["id"])
    return updated or row


async def process_ready_automation(bot, news_id: int) -> dict:
    row = await get_news(news_id)
    if not row:
        return {"row": None, "published": False, "photo_edited": False, "photo_error": "Post not found"}

    photo_edited = False
    photo_error = None
    photo_mode = await get_photo_edit_mode()
    publish_mode = await get_publish_mode()

    if photo_mode == "auto" and row.get("media_type") == "photo":
        try:
            row = await auto_edit_photo(bot, row)
            photo_edited = True
        except Exception as exc:
            photo_error = f"{type(exc).__name__}: {exc}"
            log.exception("Automatic photo editing failed news_id=%s", news_id)

    # If automatic photo editing was explicitly requested and failed, do not
    # publish an unclean source image automatically. Keep it in manual review.
    if publish_mode == "auto" and not photo_error:
        await publish_row(bot, row)
        await update_news(news_id, status="published", published_at=utc_now_db(), scheduled_at=None)
        row = await get_news(news_id)
        return {
            "row": row,
            "published": True,
            "photo_edited": photo_edited,
            "photo_error": None,
        }

    return {
        "row": row,
        "published": False,
        "photo_edited": photo_edited,
        "photo_error": photo_error,
    }
