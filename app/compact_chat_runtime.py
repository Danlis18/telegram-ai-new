import asyncio
import contextvars
import html
import logging
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from io import BytesIO

import aiosqlite
from telegram import Bot, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes

from app.config import settings
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.compact-chat")

_CAPTURE_CONTEXT: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "sports_news_capture_context", default=None
)
_cleanup_task: asyncio.Task | None = None


def _cleanup_hours() -> int:
    try:
        return max(1, int(os.getenv("BOT_CHAT_CLEANUP_HOURS", "24")))
    except Exception:
        return 24


async def _ensure_tables() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS preview_media_messages (
                news_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(news_id, position)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bot_message_cleanup (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chat_id, message_id)
            )"""
        )
        await db.commit()


async def _remember_bot_message(chat_id, message_id: int) -> None:
    try:
        chat_id = int(chat_id)
    except Exception:
        return
    # Only private user chats. Channels/groups use negative IDs and must never be auto-deleted.
    if chat_id <= 0:
        return
    try:
        await _ensure_tables()
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO bot_message_cleanup(chat_id,message_id) VALUES(?,?)",
                (chat_id, int(message_id)),
            )
            await db.commit()
    except Exception:
        log.exception("Could not remember bot message for cleanup chat=%s message=%s", chat_id, message_id)


async def _remember_preview(news_id: int, chat_id: int, messages) -> None:
    await _ensure_tables()
    async with aiosqlite.connect(settings.database_path) as db:
        for position, message in enumerate(messages):
            await db.execute(
                """INSERT OR REPLACE INTO preview_media_messages
                   (news_id,position,chat_id,message_id,updated_at)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP)""",
                (int(news_id), int(position), int(chat_id), int(message.message_id)),
            )
        await db.commit()


async def _preview_map(news_id: int) -> dict[int, dict]:
    await _ensure_tables()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM preview_media_messages WHERE news_id=? ORDER BY position",
            (int(news_id),),
        )
        return {int(row["position"]): dict(row) for row in await cur.fetchall()}


async def _set_preview(news_id: int, position: int, chat_id: int, message_id: int) -> None:
    await _ensure_tables()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO preview_media_messages
               (news_id,position,chat_id,message_id,updated_at)
               VALUES(?,?,?,?,CURRENT_TIMESTAMP)""",
            (int(news_id), int(position), int(chat_id), int(message_id)),
        )
        await db.commit()


def _install_send_tracking() -> None:
    if getattr(Bot, "_sports_news_cleanup_tracking", False):
        return

    original_send_message = Bot.send_message
    original_send_photo = Bot.send_photo
    original_send_video = Bot.send_video
    original_send_media_group = Bot.send_media_group

    async def send_message(self, chat_id, *args, **kwargs):
        message = await original_send_message(self, chat_id, *args, **kwargs)
        await _remember_bot_message(chat_id, message.message_id)
        return message

    async def send_photo(self, chat_id, *args, **kwargs):
        message = await original_send_photo(self, chat_id, *args, **kwargs)
        await _remember_bot_message(chat_id, message.message_id)
        capture = _CAPTURE_CONTEXT.get()
        if capture and int(chat_id) == capture[1]:
            await _remember_preview(capture[0], capture[1], [message])
        return message

    async def send_video(self, chat_id, *args, **kwargs):
        message = await original_send_video(self, chat_id, *args, **kwargs)
        await _remember_bot_message(chat_id, message.message_id)
        capture = _CAPTURE_CONTEXT.get()
        if capture and int(chat_id) == capture[1]:
            await _remember_preview(capture[0], capture[1], [message])
        return message

    async def send_media_group(self, chat_id, *args, **kwargs):
        messages = list(await original_send_media_group(self, chat_id, *args, **kwargs))
        for message in messages:
            await _remember_bot_message(chat_id, message.message_id)
        capture = _CAPTURE_CONTEXT.get()
        if capture and int(chat_id) == capture[1]:
            await _remember_preview(capture[0], capture[1], messages)
        return messages

    Bot.send_message = send_message
    Bot.send_photo = send_photo
    Bot.send_video = send_video
    Bot.send_media_group = send_media_group
    Bot._sports_news_cleanup_tracking = True


async def _cleanup_once(bot) -> None:
    await _ensure_tables()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_cleanup_hours())
    cutoff_db = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT chat_id,message_id FROM bot_message_cleanup WHERE created_at<=? ORDER BY created_at LIMIT 300",
            (cutoff_db,),
        )
        rows = [dict(row) for row in await cur.fetchall()]

    for row in rows:
        chat_id = int(row["chat_id"])
        message_id = int(row["message_id"])
        with suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "DELETE FROM bot_message_cleanup WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.execute(
                "DELETE FROM preview_media_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.commit()


async def _cleanup_loop(bot) -> None:
    while True:
        try:
            await _cleanup_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("24h bot-chat cleanup failed")
        await asyncio.sleep(1800)


def _utf16_index(text: str, units: int) -> int:
    if units <= 0:
        return 0
    used = 0
    for index, char in enumerate(text):
        used += len(char.encode("utf-16-le")) // 2
        if used >= units:
            return index + 1
    return len(text)


def _editor_message_html(message) -> str:
    """Render manual editor text with explicit custom-emoji entities preserved."""
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return ""

    entities = list(getattr(message, "entities", None) or [])
    if not entities:
        return getattr(message, "text_html", None) or text

    opens: dict[int, list[tuple[int, str]]] = {}
    closes: dict[int, list[tuple[int, str]]] = {}
    supported = 0
    for entity in entities:
        kind = str(getattr(entity, "type", "")).lower().split(".")[-1]
        start = _utf16_index(text, int(getattr(entity, "offset", 0)))
        end = _utf16_index(text, int(getattr(entity, "offset", 0)) + int(getattr(entity, "length", 0)))
        if start >= end:
            continue
        if kind == "bold":
            opening, closing = "<b>", "</b>"
        elif kind == "italic":
            opening, closing = "<i>", "</i>"
        elif kind in {"blockquote", "expandable_blockquote"}:
            opening, closing = "<blockquote>", "</blockquote>"
        elif kind == "custom_emoji":
            emoji_id = str(getattr(entity, "custom_emoji_id", "") or "")
            if not emoji_id.isdigit():
                continue
            opening, closing = f'<tg-emoji emoji-id="{emoji_id}">', "</tg-emoji>"
        else:
            continue
        supported += 1
        # Outer spans open first; inner spans close first.
        opens.setdefault(start, []).append((end, opening))
        closes.setdefault(end, []).append((start, closing))

    if not supported:
        return getattr(message, "text_html", None) or text

    out: list[str] = []
    for index in range(len(text) + 1):
        if index in closes:
            for _start, closing in sorted(closes[index], key=lambda pair: pair[0], reverse=True):
                out.append(closing)
        if index in opens:
            for _end, opening in sorted(opens[index], key=lambda pair: pair[0], reverse=True):
                out.append(opening)
        if index < len(text):
            out.append(html.escape(text[index]))
    return "".join(out).strip()


async def _compact_edit_media_set(bot, row: dict, user_id: int, progress_message=None):
    from app import album_support

    items = await album_support.get_news_media(int(row["id"]))
    if not items:
        raise RuntimeError("Media set is empty")
    photos = [item for item in items if item.get("media_type") == "photo"]
    if not photos:
        return row, 0, []

    mapping = await _preview_map(int(row["id"]))
    failures: list[str] = []
    edited_count = 0
    photo_number = 0
    total_media = len(items)
    total_photos = len(photos)

    for item in items:
        if item.get("media_type") != "photo":
            continue
        photo_number += 1
        if progress_message is not None:
            with suppress(Exception):
                await progress_message.edit_text(
                    f"🧹 <b>Редагую фото поста #{row['id']}</b>\n\n"
                    f"Фото <b>{photo_number}/{total_photos}</b>",
                    parse_mode="HTML",
                )

        buf = None
        try:
            source = await album_support._download_bot_file(bot, item["original_file_id"])
            edited = await album_support.generate_news_image(
                row.get("rewritten_text") or row.get("original_text") or "",
                source_image=source,
            )
            buf = BytesIO(edited)
            buf.name = f"sports_news_{row['id']}_{item['position'] + 1}.jpg"
            label = f"🖼 Фото {item['position'] + 1}/{total_media} · готово"
            preview = mapping.get(int(item["position"]))
            sent = None
            if preview and int(preview.get("chat_id") or 0) == int(user_id):
                try:
                    sent = await bot.edit_message_media(
                        chat_id=int(user_id),
                        message_id=int(preview["message_id"]),
                        media=InputMediaPhoto(media=buf, caption=label, parse_mode="HTML"),
                    )
                except Exception:
                    log.exception(
                        "Could not replace preview in-place news=%s position=%s; falling back to one new media message",
                        row["id"], item["position"],
                    )
                    sent = None

            if sent is None:
                buf.seek(0)
                sent = await bot.send_photo(int(user_id), photo=buf, caption=label, parse_mode="HTML")
                await _set_preview(int(row["id"]), int(item["position"]), int(user_id), int(sent.message_id))

            await album_support._set_media_result(
                int(item["id"]), album_support._sent_file_id(sent, "photo"), None
            )
            edited_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            failures.append(f"Фото {photo_number}/{total_photos}: {error}")
            await album_support._set_media_result(int(item["id"]), None, error)
            log.exception("Compact album edit failed news=%s media=%s", row["id"], item["id"])
        finally:
            if buf is not None:
                with suppress(Exception):
                    buf.close()

    await album_support._sync_legacy_media(int(row["id"]))
    updated = await album_support.get_news(int(row["id"]))
    return updated or row, edited_count, failures


async def _restore_preview(bot, news_id: int, user_id: int) -> None:
    from app import album_support

    items = await album_support.get_news_media(news_id)
    mapping = await _preview_map(news_id)
    for item in items:
        if item.get("media_type") != "photo":
            continue
        preview = mapping.get(int(item["position"]))
        if not preview or int(preview.get("chat_id") or 0) != int(user_id):
            continue
        with suppress(Exception):
            await bot.edit_message_media(
                chat_id=int(user_id),
                message_id=int(preview["message_id"]),
                media=InputMediaPhoto(
                    media=item["original_file_id"],
                    caption=f"🖼 Фото {item['position'] + 1}/{len(items)} · оригінал",
                    parse_mode="HTML",
                ),
            )
    await album_support._restore_media(news_id)


async def _compact_album_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.effective_user:
        return
    from app.auth import is_authorized_id
    from app import album_support

    if not is_authorized_id(update.effective_user.id):
        return
    q = update.callback_query
    data = q.data or ""
    if not (data.startswith("regen_image:") or data.startswith("restore_image:")):
        return
    news_id = int(data.split(":", 1)[1])
    user_id = int(q.from_user.id)
    await q.answer()

    with user_scope(user_id):
        row = await album_support.get_news(news_id)
        if not row:
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop

        if data.startswith("restore_image:"):
            await _restore_preview(context.bot, news_id, user_id)
            row = await album_support.get_news(news_id)
            await q.edit_message_text(
                f"↩️ <b>Для поста #{news_id} повернено всі оригінальні фото</b>",
                parse_mode="HTML",
                reply_markup=album_support._item_menu(row),
            )
            raise ApplicationHandlerStop

        items = await album_support.get_news_media(news_id)
        photos = [item for item in items if item.get("media_type") == "photo"]
        if not photos:
            await q.answer("У цьому пості немає фото", show_alert=True)
            raise ApplicationHandlerStop

        # Reuse the existing control message instead of creating a new progress/status message.
        await q.edit_message_text(
            f"🧹 <b>Редагую всі фото поста #{news_id}</b>\n\n"
            f"Починаю: <b>0/{len(photos)}</b>",
            parse_mode="HTML",
        )
        try:
            updated, edited_count, failures = await _compact_edit_media_set(
                context.bot, row, user_id, q.message
            )
            failure_text = ""
            if failures:
                failure_text = f"\n⚠️ Не вдалося: <b>{len(failures)}</b> — для них залишено оригінал."
            await q.edit_message_text(
                f"✅ <b>Фото поста #{news_id} оновлено</b>\n\n"
                f"Відредаговано: <b>{edited_count}/{len(photos)}</b>{failure_text}",
                parse_mode="HTML",
                reply_markup=album_support._item_menu(updated),
            )
        except Exception as exc:
            log.exception("Compact batch media edit failed news=%s", news_id)
            await q.edit_message_text(
                f"🔴 <b>Редагування поста #{news_id} не завершено</b>\n\n"
                f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc)[:700])}</code>",
                parse_mode="HTML",
                reply_markup=album_support._item_menu(row),
            )
        raise ApplicationHandlerStop


def _install_capture_tracking() -> None:
    from app import album_support, main as main_mod

    if getattr(album_support, "_compact_capture_tracking", False):
        return
    original_capture = album_support.capture_media

    async def capture_with_tracking(event, news_id: int, user_id: int):
        token = _CAPTURE_CONTEXT.set((int(news_id), int(user_id)))
        try:
            return await original_capture(event, news_id, user_id)
        finally:
            _CAPTURE_CONTEXT.reset(token)

    album_support.capture_media = capture_with_tracking
    main_mod.capture_media = capture_with_tracking
    album_support._compact_capture_tracking = True


def _install_editor_emoji_fix() -> None:
    from app import admin_bot

    if getattr(admin_bot, "_compact_editor_emoji_fix", False):
        return

    async def handle_editor_text_with_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await admin_bot.guard(update):
            return
        news_id = context.user_data.get("editing_news_id")
        if not news_id:
            return
        row = await admin_bot.get_news(int(news_id))
        if not row:
            context.user_data.pop("editing_news_id", None)
            await update.message.reply_text("Пост уже не знайдено.")
            return

        rendered = _editor_message_html(update.message)
        corrected = admin_bot._clean_editor_text(rendered)
        if len(re.sub(r"<[^>]+>", "", corrected).strip()) < 20:
            await update.message.reply_text(
                "Текст надто короткий. Надішли повний виправлений пост або натисни Скасувати."
            )
            return

        old_ai = row.get("rewritten_text") or ""
        await admin_bot.save_editorial_feedback(
            int(news_id), row.get("original_text") or "", old_ai, corrected
        )
        await admin_bot.update_news(int(news_id), rewritten_text=corrected, status="ready")
        context.user_data.pop("editing_news_id", None)
        row = await admin_bot.get_news(int(news_id))
        await update.message.reply_text(
            f"✅ <b>Правку збережено</b>\n\n"
            f"Premium emoji та Telegram-форматування збережені для поста #{news_id}.\n\n"
            f"{admin_bot.post_html(corrected)}",
            parse_mode="HTML",
            reply_markup=admin_bot.item_menu(row),
            disable_web_page_preview=True,
        )

    admin_bot.handle_editor_text = handle_editor_text_with_premium
    admin_bot._compact_editor_emoji_fix = True


def _install_compact_handlers() -> None:
    from app import admin_bot

    if getattr(admin_bot, "_compact_album_handlers", False):
        return
    original_register = admin_bot.register_publish_ui

    def register_with_compact(app):
        original_register(app)
        app.add_handler(
            CallbackQueryHandler(
                _compact_album_callback,
                pattern=r"^(regen_image:|restore_image:)",
            ),
            group=-30,
        )

    admin_bot.register_publish_ui = register_with_compact
    admin_bot._compact_album_handlers = True


def _install_cleanup_worker() -> None:
    global _cleanup_task
    from app import admin_bot, main as main_mod

    if getattr(admin_bot, "_compact_cleanup_worker", False):
        return
    original_start = admin_bot.start_admin_bot

    async def start_with_cleanup():
        global _cleanup_task
        app = await original_start()
        if _cleanup_task is None or _cleanup_task.done():
            _cleanup_task = app.create_task(_cleanup_loop(app.bot))
        return app

    admin_bot.start_admin_bot = start_with_cleanup
    main_mod.start_admin_bot = start_with_cleanup
    admin_bot._compact_cleanup_worker = True


def install_compact_chat_runtime() -> None:
    """Compact admin chat: in-place photo replacement, 24h cleanup, Premium emoji manual edits."""
    _install_send_tracking()
    _install_capture_tracking()
    _install_editor_emoji_fix()
    _install_compact_handlers()
    _install_cleanup_worker()

    from app import album_support
    album_support._edit_media_set = _compact_edit_media_set
    log.info(
        "Installed compact admin chat runtime: in-place media edits, %sh cleanup, Premium editor emoji",
        _cleanup_hours(),
    )
