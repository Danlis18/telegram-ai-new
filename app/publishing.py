import logging
from datetime import datetime, timezone
from io import BytesIO

from app.ai_editor import generate_news_image
from app.config import settings
from app.database import get_default_target, get_news, get_setting, update_news
from app.formatting import post_html
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.publishing")


def utc_now_db() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def get_publish_mode(user_id: int | None = None) -> str:
    with user_scope(user_id) if user_id is not None else _null_scope():
        default = "auto" if settings.auto_publish else "manual"
        value = (await get_setting("publish_mode", default) or default).lower()
        return value if value in {"auto", "manual"} else default


async def get_photo_edit_mode(user_id: int | None = None) -> str:
    with user_scope(user_id) if user_id is not None else _null_scope():
        value = (await get_setting("photo_edit_mode", "manual") or "manual").lower()
        return value if value in {"auto", "manual"} else "manual"


class _null_scope:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


async def publish_row(bot, row: dict) -> None:
    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    target = await get_default_target(user_id)
    if not target:
        raise RuntimeError("TARGET_CHANNEL_NOT_CONFIGURED: add a publishing channel in 'Мої канали'")

    destination = target["channel_ref"]
    text = post_html(row.get("rewritten_text") or "")
    media_type = row.get("media_type")
    file_id = row.get("media_file_id")

    if media_type == "photo" and file_id:
        await bot.send_photo(destination, photo=file_id, caption=text, parse_mode="HTML")
    elif media_type == "video" and file_id:
        await bot.send_video(
            destination,
            video=file_id,
            caption=text,
            parse_mode="HTML",
            supports_streaming=True,
        )
    else:
        await bot.send_message(
            destination,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def auto_edit_photo(bot, row: dict) -> dict:
    if row.get("media_type") != "photo":
        return row

    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    if not user_id:
        raise RuntimeError("AUTO_PHOTO_EDIT: user workspace missing")

    with user_scope(user_id):
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
            user_id,
            photo=upload,
            caption=f"🤖 <b>Фото #{row['id']} оброблено автоматично</b>",
            parse_mode="HTML",
        )
        edited_file_id = sent.photo[-1].file_id
        await update_news(row["id"], media_file_id=edited_file_id)
        updated = await get_news(row["id"])
        return updated or row


async def process_ready_automation(bot, news_id: int, user_id: int | None = None) -> dict:
    if user_id is None:
        row_probe = await get_news(news_id)
        user_id = int((row_probe or {}).get("user_id") or settings.admin_user_id or 0)
    if not user_id:
        return {
            "row": None,
            "published": False,
            "photo_edited": False,
            "photo_error": None,
            "publish_error": "User workspace missing",
        }

    with user_scope(user_id):
        row = await get_news(news_id)
        if not row:
            return {
                "row": None,
                "published": False,
                "photo_edited": False,
                "photo_error": "Post not found",
                "publish_error": None,
            }

        photo_edited = False
        photo_error = None
        publish_error = None
        photo_mode = await get_photo_edit_mode()
        publish_mode = await get_publish_mode()

        if photo_mode == "auto" and row.get("media_type") == "photo":
            try:
                row = await auto_edit_photo(bot, row)
                photo_edited = True
            except Exception as exc:
                photo_error = f"{type(exc).__name__}: {exc}"
                log.exception("Automatic photo editing failed news_id=%s user_id=%s", news_id, user_id)

        if publish_mode == "auto" and not photo_error:
            try:
                await publish_row(bot, row)
                await update_news(news_id, status="published", published_at=utc_now_db(), scheduled_at=None)
                row = await get_news(news_id)
                return {
                    "row": row,
                    "published": True,
                    "photo_edited": photo_edited,
                    "photo_error": None,
                    "publish_error": None,
                }
            except Exception as exc:
                publish_error = f"{type(exc).__name__}: {exc}"
                log.exception("Automatic publish failed news_id=%s user_id=%s", news_id, user_id)

        return {
            "row": row,
            "published": False,
            "photo_edited": photo_edited,
            "photo_error": photo_error,
            "publish_error": publish_error,
        }
