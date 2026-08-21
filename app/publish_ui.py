import asyncio
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.database import (
    get_due_scheduled,
    get_news,
    get_scheduled,
    get_setting,
    set_setting,
    update_news,
)
from app.formatting import post_html
from app.publishing import get_photo_edit_mode, get_publish_mode, publish_row, utc_now_db

log = logging.getLogger("telegram-ai-news.publish-ui")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.publish_timezone)
    except Exception:
        return ZoneInfo("Europe/Kyiv")


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and settings.admin_user_id and user.id == settings.admin_user_id)


def _preview_buttons(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Опублікувати зараз", callback_data=f"publish_now:{news_id}")],
        [InlineKeyboardButton("📅 Запланувати", callback_data=f"schedule:{news_id}")],
        [InlineKeyboardButton("❌ Закрити прев’ю", callback_data=f"preview_close:{news_id}")],
    ])


async def show_control_panel(query) -> None:
    paused = (await get_setting("processing_paused", "false")) == "true"
    publish_mode = await get_publish_mode()
    photo_mode = await get_photo_edit_mode()

    publish_label = "🤖 Автопостинг" if publish_mode == "auto" else "👤 Ручна публікація"
    photo_label = "🤖 Автообробка фото" if photo_mode == "auto" else "👤 Ручна обробка фото"
    pause_label = "▶️ Відновити reader" if paused else "⏸ Призупинити reader"

    await query.edit_message_text(
        "⚙️ <b>Керування SPORTS NEWS</b>\n\n"
        f"Reader: <b>{'⏸ ПРИЗУПИНЕНО' if paused else '🟢 ПРАЦЮЄ'}</b>\n"
        f"Публікація: <b>{'АВТО' if publish_mode == 'auto' else 'РУЧНА'}</b>\n"
        f"Обробка фото: <b>{'АВТО' if photo_mode == 'auto' else 'РУЧНА'}</b>\n"
        f"Поріг AI: <b>{settings.min_publish_score}%</b>\n"
        f"Часовий пояс планування: <b>{html.escape(settings.publish_timezone)}</b>\n\n"
        "У ручному режимі перед публікацією бот показує фінальне прев’ю. "
        "Зміни режимів застосовуються до нових готових постів.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(publish_label, callback_data=f"set_publish_mode:{'manual' if publish_mode == 'auto' else 'auto'}")],
            [InlineKeyboardButton(photo_label, callback_data=f"set_photo_mode:{'manual' if photo_mode == 'auto' else 'auto'}")],
            [InlineKeyboardButton(pause_label, callback_data="publish_ui_toggle_pause")],
            [InlineKeyboardButton("📅 Заплановані", callback_data="scheduled_list"), InlineKeyboardButton("✅ Готові пости", callback_data="queue")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]),
    )


async def control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    await q.answer()
    await show_control_panel(q)
    raise ApplicationHandlerStop


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("set_publish_mode:"):
        mode = data.split(":", 1)[1]
        if mode in {"auto", "manual"}:
            await set_setting("publish_mode", mode)
    elif data.startswith("set_photo_mode:"):
        mode = data.split(":", 1)[1]
        if mode in {"auto", "manual"}:
            await set_setting("photo_edit_mode", mode)
    elif data == "publish_ui_toggle_pause":
        paused = (await get_setting("processing_paused", "false")) == "true"
        await set_setting("processing_paused", "false" if paused else "true")

    await show_control_panel(q)
    raise ApplicationHandlerStop


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, row: dict) -> None:
    chat_id = settings.admin_user_id
    text = post_html(row.get("rewritten_text") or "")
    buttons = _preview_buttons(row["id"])

    await context.bot.send_message(
        chat_id,
        f"👁 <b>Фінальне прев’ю поста #{row['id']}</b>\n"
        "Нижче він виглядає так само, як буде виглядати в каналі.",
        parse_mode="HTML",
    )

    if row.get("media_type") == "photo" and row.get("media_file_id"):
        await context.bot.send_photo(
            chat_id,
            photo=row["media_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=buttons,
        )
    elif row.get("media_type") == "video" and row.get("media_file_id"):
        await context.bot.send_video(
            chat_id,
            video=row["media_file_id"],
            caption=text,
            parse_mode="HTML",
            supports_streaming=True,
            reply_markup=buttons,
        )
    else:
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=buttons,
        )


async def publish_flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data.startswith("publish:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if not row or not row.get("rewritten_text"):
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop
        await _send_preview(context, row)
        raise ApplicationHandlerStop

    if data.startswith("publish_now:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if not row or not row.get("rewritten_text"):
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop
        await publish_row(context.bot, row)
        await update_news(news_id, status="published", published_at=utc_now_db(), scheduled_at=None)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            settings.admin_user_id,
            f"✅ <b>Пост #{news_id} опубліковано зараз</b>",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    if data.startswith("schedule:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if not row:
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop
        context.user_data["scheduling_news_id"] = news_id
        await context.bot.send_message(
            settings.admin_user_id,
            f"📅 <b>Планування поста #{news_id}</b>\n\n"
            f"Надішли дату й час у форматі:\n<code>21.08.2026 18:30</code>\n\n"
            f"Часовий пояс: <b>{html.escape(settings.publish_timezone)}</b>",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    if data.startswith("preview_close:"):
        try:
            await q.message.delete()
        except Exception:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        raise ApplicationHandlerStop


async def schedule_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    news_id = context.user_data.get("scheduling_news_id")
    if not news_id or not _is_admin(update):
        return

    raw = (update.message.text or "").strip()
    try:
        local_dt = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=_tz())
    except ValueError:
        await update.message.reply_text(
            "❌ Невірний формат. Надішли так: <code>21.08.2026 18:30</code>",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    now_local = datetime.now(_tz())
    if local_dt <= now_local:
        await update.message.reply_text("❌ Час публікації має бути в майбутньому.")
        raise ApplicationHandlerStop

    utc_dt = local_dt.astimezone(timezone.utc)
    scheduled_at = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
    await update_news(int(news_id), status="scheduled", scheduled_at=scheduled_at)
    context.user_data.pop("scheduling_news_id", None)

    await update.message.reply_text(
        f"✅ <b>Пост #{news_id} заплановано</b>\n\n"
        f"📅 {local_dt.strftime('%d.%m.%Y')}\n"
        f"🕒 {local_dt.strftime('%H:%M')} ({html.escape(settings.publish_timezone)})",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Заплановані", callback_data="scheduled_list")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]),
    )
    raise ApplicationHandlerStop


async def scheduled_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    await q.answer()
    rows = await get_scheduled(20)
    if not rows:
        await q.edit_message_text(
            "📅 <b>Запланованих постів немає</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Налаштування", callback_data="control")]]),
        )
        raise ApplicationHandlerStop

    lines = []
    for row in rows:
        try:
            utc_dt = datetime.strptime(row["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            local_dt = utc_dt.astimezone(_tz())
            when = local_dt.strftime("%d.%m %H:%M")
        except Exception:
            when = row.get("scheduled_at") or "—"
        lines.append(f"• <b>#{row['id']}</b> — {html.escape(when)}")

    await q.edit_message_text(
        "📅 <b>Заплановані публікації</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Налаштування", callback_data="control")]]),
    )
    raise ApplicationHandlerStop


async def scheduled_publish_worker(app: Application) -> None:
    while True:
        try:
            rows = await get_due_scheduled(utc_now_db(), 20)
            for row in rows:
                try:
                    await publish_row(app.bot, row)
                    await update_news(row["id"], status="published", published_at=utc_now_db(), scheduled_at=None)
                    if settings.admin_user_id:
                        await app.bot.send_message(
                            settings.admin_user_id,
                            f"✅ <b>Запланований пост #{row['id']} опубліковано</b>",
                            parse_mode="HTML",
                        )
                except Exception as exc:
                    log.exception("Scheduled publish failed news_id=%s", row.get("id"))
                    if settings.admin_user_id:
                        await app.bot.send_message(
                            settings.admin_user_id,
                            f"🔴 <b>Не вдалося опублікувати запланований пост #{row.get('id')}</b>\n"
                            f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc)[:800])}</code>",
                            parse_mode="HTML",
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduled publisher loop failed")
        await asyncio.sleep(20)


def register_publish_ui(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(control_callback, pattern=r"^control$"), group=-1)
    app.add_handler(CallbackQueryHandler(mode_callback, pattern=r"^(set_publish_mode:|set_photo_mode:|publish_ui_toggle_pause$)"), group=-1)
    app.add_handler(CallbackQueryHandler(publish_flow_callback, pattern=r"^(publish:|publish_now:|schedule:|preview_close:)"), group=-1)
    app.add_handler(CallbackQueryHandler(scheduled_list_callback, pattern=r"^scheduled_list$"), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_text_handler), group=-1)


def start_publish_worker(app: Application) -> None:
    old = app.bot_data.get("scheduled_publish_task")
    if old and not old.done():
        return
    app.bot_data["scheduled_publish_task"] = asyncio.create_task(scheduled_publish_worker(app))


async def stop_publish_worker(app: Application) -> None:
    task = app.bot_data.get("scheduled_publish_task")
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
