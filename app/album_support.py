import asyncio
import html
import logging
from contextlib import suppress
from io import BytesIO
from types import SimpleNamespace

import aiosqlite
from telegram import InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes

from app.config import settings
from app.database import get_default_target, get_news, get_setting
from app.formatting import post_html
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.album-support")

_album_buffers: dict[tuple[int, str, int], dict] = {}
_album_buffer_lock = asyncio.Lock()


async def ensure_media_schema() -> None:
    """Create media storage and migrate existing one-photo posts without data loss."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS news_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                source_message_id INTEGER,
                media_type TEXT NOT NULL,
                original_file_id TEXT NOT NULL,
                edited_file_id TEXT,
                edit_status TEXT NOT NULL DEFAULT 'original',
                edit_error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(news_id, position)
            )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_news_media_news ON news_media(news_id,position)")
        await db.execute(
            """INSERT OR IGNORE INTO news_media
               (news_id,position,source_message_id,media_type,original_file_id,edited_file_id,edit_status)
               SELECT id,0,source_message_id,
                      CASE WHEN media_type='video' THEN 'video' ELSE 'photo' END,
                      original_media_file_id,
                      CASE
                        WHEN media_file_id IS NOT NULL AND media_file_id<>original_media_file_id THEN media_file_id
                        ELSE NULL
                      END,
                      CASE
                        WHEN media_file_id IS NOT NULL AND media_file_id<>original_media_file_id THEN 'edited'
                        ELSE 'original'
                      END
               FROM news
               WHERE original_media_file_id IS NOT NULL"""
        )
        await db.commit()


async def get_news_media(news_id: int) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM news_media WHERE news_id=? ORDER BY position ASC",
            (int(news_id),),
        )
        return [dict(row) for row in await cur.fetchall()]


async def _replace_news_media(news_id: int, items: list[dict]) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("DELETE FROM news_media WHERE news_id=?", (int(news_id),))
        for position, item in enumerate(items):
            await db.execute(
                """INSERT INTO news_media
                   (news_id,position,source_message_id,media_type,original_file_id,edited_file_id,edit_status,edit_error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    int(news_id),
                    position,
                    item.get("source_message_id"),
                    item["media_type"],
                    item["original_file_id"],
                    item.get("edited_file_id"),
                    item.get("edit_status") or "original",
                    item.get("edit_error"),
                ),
            )
        await db.commit()
    await _sync_legacy_media(news_id)


async def _set_media_result(media_id: int, edited_file_id: str | None, error: str | None = None) -> None:
    status = "edited" if edited_file_id else ("error" if error else "original")
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE news_media
               SET edited_file_id=?, edit_status=?, edit_error=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (edited_file_id, status, error, int(media_id)),
        )
        await db.commit()


async def _restore_media(news_id: int) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE news_media
               SET edited_file_id=NULL, edit_status='original', edit_error=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE news_id=?""",
            (int(news_id),),
        )
        await db.commit()
    await _sync_legacy_media(news_id)


def _current_file_id(item: dict) -> str:
    return item.get("edited_file_id") or item["original_file_id"]


async def _sync_legacy_media(news_id: int) -> None:
    items = await get_news_media(news_id)
    if not items:
        return
    photo = next((item for item in items if item.get("media_type") == "photo"), None)
    legacy = photo or items[0]
    legacy_type = "photo" if photo else legacy.get("media_type", "video")
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE news
               SET media_type=?, media_file_id=?, original_media_file_id=?
               WHERE id=?""",
            (
                legacy_type,
                _current_file_id(legacy),
                legacy["original_file_id"],
                int(news_id),
            ),
        )
        await db.commit()


def _message_media_type(message) -> str | None:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    return None


async def _download_telethon_media(message, kind: str) -> bytes:
    last_error = None
    for attempt in range(2):
        try:
            data = await message.download_media(file=bytes)
            if data:
                return bytes(data)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.5)
    if last_error:
        raise last_error
    raise RuntimeError(f"Telegram reader returned empty {kind}")


def _upload_media(kind: str, value, caption: str | None = None, parse_mode: str | None = None):
    if kind == "video":
        return InputMediaVideo(
            media=value,
            caption=caption,
            parse_mode=parse_mode,
            supports_streaming=True,
        )
    return InputMediaPhoto(media=value, caption=caption, parse_mode=parse_mode)


def _sent_file_id(message, kind: str) -> str:
    if kind == "video":
        if not getattr(message, "video", None):
            raise RuntimeError("Telegram did not return a video file_id")
        return message.video.file_id
    if not getattr(message, "photo", None):
        raise RuntimeError("Telegram did not return a photo file_id")
    return message.photo[-1].file_id


async def capture_media(event, news_id: int, user_id: int) -> str | None:
    """Capture every photo/video from a source album as one news entity."""
    carrier = getattr(event, "message", event)
    messages = list(getattr(event, "album_messages", None) or [carrier])
    messages = sorted(messages, key=lambda message: int(getattr(message, "id", 0)))
    supported = [(message, _message_media_type(message)) for message in messages]
    supported = [(message, kind) for message, kind in supported if kind in {"photo", "video"}]
    if not supported:
        return None

    payload = []
    buffers: list[BytesIO] = []
    metadata = []
    total = len(supported)
    try:
        for index, (message, kind) in enumerate(supported, 1):
            data = await _download_telethon_media(message, kind)
            buf = BytesIO(data)
            buf.name = f"source_{news_id}_{index}.{'mp4' if kind == 'video' else 'jpg'}"
            buffers.append(buf)
            label = f"{'🎥 Відео' if kind == 'video' else '🖼 Фото'} {index}/{total} · оригінал"
            payload.append(_upload_media(kind, buf, label, "HTML"))
            metadata.append(
                {
                    "source_message_id": int(getattr(message, "id", 0)),
                    "media_type": kind,
                }
            )

        if len(payload) == 1:
            kind = metadata[0]["media_type"]
            if kind == "video":
                sent_one = await publisher_send_video(user_id, buffers[0], "🎥 <b>Оригінальне відео з джерела</b>")
            else:
                sent_one = await publisher_send_photo(user_id, buffers[0], "🖼 <b>Оригінальне фото з джерела</b>")
            sent_messages = [sent_one]
        else:
            from app import main as main_mod
            sent_messages = await main_mod.publisher.send_media_group(int(user_id), media=payload)

        stored = []
        for meta, sent_message in zip(metadata, sent_messages):
            file_id = _sent_file_id(sent_message, meta["media_type"])
            stored.append(
                {
                    **meta,
                    "original_file_id": file_id,
                    "edited_file_id": None,
                    "edit_status": "original",
                    "edit_error": None,
                }
            )
        if len(stored) != len(metadata):
            raise RuntimeError("Telegram returned incomplete media group")

        await _replace_news_media(news_id, stored)
        photo_count = sum(1 for item in stored if item["media_type"] == "photo")
        video_count = len(stored) - photo_count
        log.info(
            "Captured media news_id=%s user=%s total=%s photos=%s videos=%s",
            news_id,
            user_id,
            len(stored),
            photo_count,
            video_count,
        )
        return "photo" if photo_count else "video"
    except Exception:
        log.exception("Failed to capture media set news_id=%s user_id=%s", news_id, user_id)
        return None
    finally:
        for buf in buffers:
            with suppress(Exception):
                buf.close()


async def publisher_send_photo(user_id: int, photo, caption: str):
    from app import main as main_mod
    return await main_mod.publisher.send_photo(int(user_id), photo=photo, caption=caption, parse_mode="HTML")


async def publisher_send_video(user_id: int, video, caption: str):
    from app import main as main_mod
    return await main_mod.publisher.send_video(
        int(user_id),
        video=video,
        caption=caption,
        parse_mode="HTML",
        supports_streaming=True,
    )


async def _send_file_id_media_set(
    bot,
    chat_id,
    items: list[dict],
    *,
    original: bool = False,
    caption: str | None = None,
    parse_mode: str | None = None,
    labels: bool = False,
):
    if not items:
        return []

    total = len(items)
    payload = []
    for index, item in enumerate(items, 1):
        kind = item["media_type"]
        file_id = item["original_file_id"] if original else _current_file_id(item)
        item_caption = caption if index == 1 else None
        item_parse_mode = parse_mode if index == 1 else None
        if labels:
            item_caption = f"{'🎥 Відео' if kind == 'video' else '🖼 Фото'} {index}/{total} · {'оригінал' if original else 'готово'}"
            item_parse_mode = "HTML"
        payload.append(_upload_media(kind, file_id, item_caption, item_parse_mode))

    if total > 1:
        return list(await bot.send_media_group(chat_id, media=payload))

    item = items[0]
    file_id = item["original_file_id"] if original else _current_file_id(item)
    single_caption = caption
    single_parse_mode = parse_mode
    if labels:
        single_caption = f"{'🎥 Відео' if item['media_type'] == 'video' else '🖼 Фото'} 1/1 · {'оригінал' if original else 'готово'}"
        single_parse_mode = "HTML"
    if item["media_type"] == "video":
        sent = await bot.send_video(
            chat_id,
            video=file_id,
            caption=single_caption,
            parse_mode=single_parse_mode,
            supports_streaming=True,
        )
    else:
        sent = await bot.send_photo(
            chat_id,
            photo=file_id,
            caption=single_caption,
            parse_mode=single_parse_mode,
        )
    return [sent]


async def publish_row(bot, row: dict) -> None:
    items = await get_news_media(int(row["id"]))
    if not items:
        return await _original_publish_row(bot, row)

    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    target = await get_default_target(user_id)
    if not target:
        raise RuntimeError("TARGET_CHANNEL_NOT_CONFIGURED: add a publishing channel in 'Мої канали'")
    destination = target["channel_ref"]
    text = post_html(row.get("rewritten_text") or "")
    await _send_file_id_media_set(bot, destination, items, caption=text, parse_mode="HTML")


async def _clean_edit_source_photo(news_text: str, source_image: bytes) -> bytes:
    """Edit only clear third-party branding while preserving sports result graphics."""
    from app import ai_editor

    clean_context = ai_editor.re.sub(r"<[^>]+>", " ", news_text)[:1000]
    image_model = ai_editor._effective_image_model()
    source_image = ai_editor._normalize_to_jpeg(source_image, "SOURCE_IMAGE")
    owner_image_prompt = (await get_setting("image_prompt_custom", "") or "").strip()
    owner_rules = f" Additional owner instructions: {owner_image_prompt}" if owner_image_prompt else ""
    image_file = BytesIO(source_image)
    image_file.name = "source.jpg"

    try:
        result = await ai_editor.client.images.edit(
            model=image_model,
            image=image_file,
            prompt=(
                "EDIT THIS EXACT SOURCE IMAGE; DO NOT CREATE A NEW SCENE. "
                "Remove ONLY clear third-party advertising or foreign media branding that is not part of the sports information: "
                "media/channel watermarks, PRESENTED BY blocks, publisher marks, bookmaker/casino/betting branding, promo/CTA blocks and unrelated sponsor overlays added on top of the image. "
                "PRESERVE sports editorial graphics and match information exactly as they are: team logos and crests, score, fixture/result, FULL-TIME or match-status text, competition/tournament logos, dates, kick-off times, player names and sports statistics when they belong to the match graphic. "
                "Also preserve all genuine details photographed on clothing and equipment, including club crests, jersey sponsors, numbers, manufacturer logos and patches. "
                "If an overlay could reasonably be sports information rather than advertising, KEEP IT. Remove only branding that is clearly an external ad, publisher watermark or promo. "
                "Reconstruct only the small areas hidden behind removed branding so they naturally match the surrounding original background. "
                "Preserve the same real person, face, expression, body, pose, clothing, crop, camera angle, stadium/background, colors, lighting, shadows and photographic texture. "
                "Do not redesign, restyle, recolor, relight, beautify, sharpen, change anatomy, replace people or add new text. "
                f"News context is for identification only and must NOT be rendered as text: {clean_context}."
                + owner_rules
            ),
        )
    except Exception as exc:
        await ai_editor.increment_generation_stat("image_errors")
        raise RuntimeError(f"IMAGE_EDIT_API_FAILED[{image_model}]: {type(exc).__name__}: {exc}") from exc

    if not result.data:
        await ai_editor.increment_generation_stat("image_errors")
        raise RuntimeError(f"IMAGE_EDIT_EMPTY_RESULT[{image_model}]")
    edited_bytes = await ai_editor._image_result_bytes(result.data[0])
    if not edited_bytes:
        await ai_editor.increment_generation_stat("image_errors")
        raise RuntimeError(f"IMAGE_EDIT_RESULT_HAS_NO_IMAGE[{image_model}]")
    working = ai_editor._restore_source_dimensions(edited_bytes, source_image)
    await ai_editor.increment_generation_stat("image_edits")
    return await ai_editor._finalize_image(working)


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key=None) -> bytes:
    if source_image is not None:
        return await _clean_edit_source_photo(news_text, source_image)
    kwargs = {}
    if template_key is not None:
        kwargs["template_key"] = template_key
    return await _original_generate_news_image(news_text, **kwargs)


async def _download_bot_file(bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    data = await tg_file.download_as_bytearray()
    if not data:
        raise RuntimeError("Telegram returned empty media bytes")
    return bytes(data)


async def _edit_media_set(bot, row: dict, user_id: int, progress_message=None) -> tuple[dict, int, list[str]]:
    items = await get_news_media(int(row["id"]))
    if not items:
        raise RuntimeError("Media set is empty")

    photo_items = [item for item in items if item["media_type"] == "photo"]
    if not photo_items:
        return row, 0, []

    payload = []
    payload_meta = []
    open_buffers: list[BytesIO] = []
    failures: list[str] = []
    edited_count = 0
    photo_index = 0
    photo_total = len(photo_items)

    for item in items:
        if item["media_type"] == "video":
            payload.append(_upload_media("video", item["original_file_id"]))
            payload_meta.append((item, False, None))
            continue

        photo_index += 1
        if progress_message is not None:
            with suppress(Exception):
                await progress_message.edit_text(
                    f"🧹 <b>Редагую всі фото поста #{row['id']}</b>\n\n"
                    f"Фото <b>{photo_index}/{photo_total}</b> · медіа {item['position'] + 1}/{len(items)}\n"
                    "Зберігаю спортивну графіку, рахунок і логотипи команд; прибираю тільки сторонню рекламу/водяні знаки.",
                    parse_mode="HTML",
                )
        try:
            source = await _download_bot_file(bot, item["original_file_id"])
            edited = await generate_news_image(
                row.get("rewritten_text") or row.get("original_text") or "",
                source_image=source,
            )
            buf = BytesIO(edited)
            buf.name = f"sports_news_{row['id']}_{item['position'] + 1}.jpg"
            open_buffers.append(buf)
            payload.append(_upload_media("photo", buf))
            payload_meta.append((item, True, None))
            edited_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            failures.append(f"Фото {photo_index}/{photo_total}: {error}")
            payload.append(_upload_media("photo", item["original_file_id"]))
            payload_meta.append((item, False, error))
            log.exception("Album photo edit failed news_id=%s media_id=%s", row["id"], item["id"])

    total = len(payload)
    if total > 1:
        for index, media in enumerate(payload, 1):
            media.caption = f"{'🎥 Відео' if items[index - 1]['media_type'] == 'video' else '🖼 Фото'} {index}/{total} · готово"
            media.parse_mode = "HTML"
        sent_messages = list(await bot.send_media_group(int(user_id), media=payload))
    else:
        item, success, error = payload_meta[0]
        media_value = payload[0].media
        if item["media_type"] == "video":
            sent_messages = [await bot.send_video(int(user_id), video=media_value, caption="🎥 Відео 1/1 · готово", supports_streaming=True)]
        else:
            sent_messages = [await bot.send_photo(int(user_id), photo=media_value, caption="🖼 Фото 1/1 · готово")]

    try:
        for (item, success, error), sent_message in zip(payload_meta, sent_messages):
            if item["media_type"] == "video":
                await _set_media_result(item["id"], None, None)
                continue
            if success:
                await _set_media_result(item["id"], _sent_file_id(sent_message, "photo"), None)
            else:
                await _set_media_result(item["id"], None, error)
        await _sync_legacy_media(int(row["id"]))
    finally:
        for buf in open_buffers:
            with suppress(Exception):
                buf.close()

    updated = await get_news(int(row["id"]))
    return updated or row, edited_count, failures


async def auto_edit_photo(bot, row: dict) -> dict:
    items = await get_news_media(int(row["id"]))
    if not items:
        return await _original_auto_edit_photo(bot, row)
    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    updated, edited_count, failures = await _edit_media_set(bot, row, user_id)
    if failures:
        log.warning(
            "Auto media edit completed with partial failures news_id=%s edited=%s failures=%s",
            row["id"],
            edited_count,
            len(failures),
        )
    return updated


def _item_menu(row: dict) -> InlineKeyboardMarkup:
    news_id = row["id"]
    rows = [
        [InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{news_id}")],
        [
            InlineKeyboardButton("✏️ Редагувати текст", callback_data=f"edit:{news_id}"),
            InlineKeyboardButton("🔄 Інший варіант", callback_data=f"regen:{news_id}"),
        ],
        [InlineKeyboardButton("👁 Оригінал", callback_data=f"original:{news_id}")],
    ]
    if row.get("media_type") == "photo":
        rows.append([InlineKeyboardButton("🧹 Відредагувати всі фото", callback_data=f"regen_image:{news_id}")])
        rows.append([InlineKeyboardButton("↩️ Повернути всі оригінали", callback_data=f"restore_image:{news_id}")])
    rows.extend(
        [
            [InlineKeyboardButton("⏭ Пропустити", callback_data=f"skip:{news_id}")],
            [InlineKeyboardButton("⬅️ Готові пости", callback_data="queue"), InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, row: dict, chat_id: int) -> None:
    items = await get_news_media(int(row["id"]))
    if len(items) <= 1:
        return await _original_send_preview(context, row, chat_id)

    from app import publish_ui

    text = post_html(row.get("rewritten_text") or "")
    target = await get_default_target(int(row.get("user_id") or chat_id))
    target_name = (target or {}).get("title") or (target or {}).get("channel_ref") or "не налаштовано"
    photo_count = sum(1 for item in items if item["media_type"] == "photo")
    video_count = len(items) - photo_count
    media_summary = f"{photo_count} фото" + (f" + {video_count} відео" if video_count else "")

    await context.bot.send_message(
        chat_id,
        f"👁 <b>Фінальне прев’ю поста #{row['id']}</b>\n"
        f"📺 Канал: <b>{html.escape(str(target_name))}</b>\n"
        f"🗂 Альбом: <b>{html.escape(media_summary)}</b>\n\n"
        "Нижче весь комплект у правильному порядку.",
        parse_mode="HTML",
    )
    await _send_file_id_media_set(context.bot, chat_id, items, caption=text, parse_mode="HTML")
    await context.bot.send_message(
        chat_id,
        "Керування цим альбомом:",
        reply_markup=publish_ui._preview_buttons(int(row["id"])),
    )


async def _album_media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    from app.auth import is_authorized_id
    if not is_authorized_id(update.effective_user.id):
        return

    q = update.callback_query
    data = q.data or ""
    news_id = int(data.split(":", 1)[1])
    user_id = int(q.from_user.id)
    await q.answer()

    with user_scope(user_id):
        row = await get_news(news_id)
        if not row:
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop
        items = await get_news_media(news_id)

        if data.startswith("original:"):
            if items:
                await context.bot.send_message(
                    user_id,
                    f"👁 <b>Оригінал поста #{news_id}</b> · медіа: <b>{len(items)}</b>",
                    parse_mode="HTML",
                )
                await _send_file_id_media_set(context.bot, user_id, items, original=True, labels=True)
            await context.bot.send_message(
                user_id,
                html.escape((row.get("original_text") or "—")[:3500]),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ До поста", callback_data=f"item:{news_id}")]]),
            )
            raise ApplicationHandlerStop

        if data.startswith("restore_image:"):
            await _restore_media(news_id)
            row = await get_news(news_id)
            await q.edit_message_text(
                f"↩️ <b>Для поста #{news_id} повернено всі оригінальні медіа</b>",
                parse_mode="HTML",
                reply_markup=_item_menu(row),
            )
            raise ApplicationHandlerStop

        if data.startswith("regen_image:"):
            if not items or not any(item["media_type"] == "photo" for item in items):
                await q.answer("У цьому пості немає фото", show_alert=True)
                raise ApplicationHandlerStop
            progress = await context.bot.send_message(
                user_id,
                f"🧹 <b>Редагую всі фото поста #{news_id}</b>\n\nПочинаю пакетну обробку…",
                parse_mode="HTML",
            )
            try:
                updated, edited_count, failures = await _edit_media_set(context.bot, row, user_id, progress)
                total_photos = sum(1 for item in items if item["media_type"] == "photo")
                failure_text = ""
                if failures:
                    short = "\n".join(f"• {html.escape(line[:500])}" for line in failures[:5])
                    failure_text = f"\n\n⚠️ <b>Не вдалося:</b> {len(failures)}\n{short}\nДля них залишено оригінал."
                with suppress(Exception):
                    await progress.edit_text(
                        f"✅ <b>Пакетна обробка поста #{news_id} завершена</b>\n\n"
                        f"Фото відредаговано: <b>{edited_count}/{total_photos}</b>\n"
                        f"Відео залишено без змін: <b>{sum(1 for item in items if item['media_type'] == 'video')}</b>"
                        f"{failure_text}",
                        parse_mode="HTML",
                        reply_markup=_item_menu(updated),
                    )
            except Exception as exc:
                log.exception("Batch media edit failed news_id=%s", news_id)
                await progress.edit_text(
                    f"🔴 <b>Пакетну обробку поста #{news_id} не завершено</b>\n\n"
                    f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc)[:900])}</code>\n\n"
                    "Оригінальні медіа збережені.",
                    parse_mode="HTML",
                    reply_markup=_item_menu(row),
                )
            raise ApplicationHandlerStop


async def _publish_news_fallback(context, row: dict):
    await publish_row(context.bot, row)


async def _flush_album(key: tuple[int, str, int]) -> None:
    await asyncio.sleep(1.25)
    async with _album_buffer_lock:
        entry = _album_buffers.pop(key, None)
    if not entry:
        return

    messages = sorted(entry["messages"].values(), key=lambda message: int(getattr(message, "id", 0)))
    if not messages:
        return
    carrier = next((message for message in messages if getattr(message, "photo", None)), messages[0])
    raw_text = ""
    for message in messages:
        candidate = (getattr(message, "raw_text", None) or "").strip()
        if candidate:
            raw_text = candidate
            break
    event = SimpleNamespace(
        message=carrier,
        raw_text=raw_text,
        id=min(int(getattr(message, "id", 0)) for message in messages),
        album_messages=messages,
    )

    from app import main as main_mod
    try:
        await main_mod.process_message(
            event,
            entry["source"],
            entry["user_id"],
            _album_complete=True,
            **entry["kwargs"],
        )
    except Exception:
        log.exception("Album flush failed source=@%s user=%s grouped_id=%s", entry["source"], entry["user_id"], key[2])


async def _album_aware_process(original, event, source: str, user_id: int, **kwargs):
    if kwargs.pop("_album_complete", False):
        return await original(event, source, user_id, **kwargs)

    message = getattr(event, "message", event)
    grouped_id = getattr(message, "grouped_id", None)
    if not grouped_id:
        return await original(event, source, user_id, **kwargs)

    key = (int(user_id), str(source).lower(), int(grouped_id))
    async with _album_buffer_lock:
        entry = _album_buffers.get(key)
        if entry is None:
            entry = {
                "source": source,
                "user_id": int(user_id),
                "messages": {},
                "kwargs": dict(kwargs),
                "task": None,
            }
            _album_buffers[key] = entry
        entry["messages"][int(getattr(message, "id", 0))] = message
        entry["kwargs"].update(kwargs)
        old_task = entry.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        entry["task"] = asyncio.create_task(_flush_album(key))
    return None


async def _album_policy_wrapper(original_policy, original, event, source: str, user_id: int, **kwargs):
    message = getattr(event, "message", event)
    grouped_id = getattr(message, "grouped_id", None)
    if grouped_id and not kwargs.get("_album_complete", False):
        # Buffer raw album pieces first. Quota/cooldown/rejection logic will run once
        # for the completed album after the debounce window.
        return await original(event, source, user_id, **kwargs)
    return await original_policy(original, event, source, user_id, **kwargs)


def _register_album_handlers(original_register, app) -> None:
    original_register(app)
    app.add_handler(
        CallbackQueryHandler(
            _album_media_callback,
            pattern=r"^(regen_image:|restore_image:|original:)",
        ),
        group=-2,
    )


def install_album_support() -> None:
    """Install album support as runtime patches without disturbing existing bot logic."""
    global _original_publish_row, _original_auto_edit_photo, _original_send_preview, _original_generate_news_image

    from app import admin_bot, ai_editor, main as main_mod, publish_ui, publishing, reader_policy

    if getattr(main_mod, "_album_support_installed", False):
        return

    _original_publish_row = publishing.publish_row
    _original_auto_edit_photo = publishing.auto_edit_photo
    _original_send_preview = publish_ui._send_preview
    _original_generate_news_image = ai_editor.generate_news_image

    original_init_db = main_mod.init_db

    async def init_db_with_media():
        await original_init_db()
        await ensure_media_schema()

    original_process = main_mod.process_message

    async def album_process(event, source: str, user_id: int, **kwargs):
        return await _album_aware_process(original_process, event, source, user_id, **kwargs)

    original_policy = reader_policy._policy_process

    async def policy_process(original, event, source: str, user_id: int, **kwargs):
        return await _album_policy_wrapper(original_policy, original, event, source, user_id, **kwargs)

    original_register_publish_ui = admin_bot.register_publish_ui

    def register_publish_ui_with_album(app):
        return _register_album_handlers(original_register_publish_ui, app)

    main_mod.init_db = init_db_with_media
    main_mod.capture_media = capture_media
    main_mod.process_message = album_process
    reader_policy._policy_process = policy_process

    publishing.publish_row = publish_row
    publishing.auto_edit_photo = auto_edit_photo
    publish_ui.publish_row = publish_row
    publish_ui._send_preview = _send_preview

    ai_editor.generate_news_image = generate_news_image
    admin_bot.generate_news_image = generate_news_image
    publishing.generate_news_image = generate_news_image
    with suppress(Exception):
        from app import multiuser_ui
        multiuser_ui.generate_news_image = generate_news_image

    admin_bot.item_menu = _item_menu
    admin_bot.publish_news = _publish_news_fallback
    admin_bot.register_publish_ui = register_publish_ui_with_album

    main_mod._album_support_installed = True
    log.info("Installed multi-photo/video album capture, batch editing and publishing support")
