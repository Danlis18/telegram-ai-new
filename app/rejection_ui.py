import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from app.auth import is_authorized_id
from app.content_policy import (
    create_replacement_request,
    current_period,
    rejection_reason_label,
    save_rejection_feedback,
)
from app.database import get_news, update_news


def _reason_buttons(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🕒 Неактуальна", callback_data=f"reject_reason:{news_id}:stale"),
            InlineKeyboardButton("😐 Слабка", callback_data=f"reject_reason:{news_id}:weak"),
        ],
        [
            InlineKeyboardButton("🔁 Повтор", callback_data=f"reject_reason:{news_id}:duplicate"),
            InlineKeyboardButton("🎯 Не та тема", callback_data=f"reject_reason:{news_id}:topic"),
        ],
        [InlineKeyboardButton("📡 Слабке джерело", callback_data=f"reject_reason:{news_id}:bad_source")],
        [InlineKeyboardButton("✍️ Напишу свою причину", callback_data=f"reject_custom:{news_id}")],
        [InlineKeyboardButton("❌ Скасувати", callback_data=f"item:{news_id}")],
    ])


async def _complete_rejection(context: ContextTypes.DEFAULT_TYPE, row: dict, user_id: int, reason: str) -> None:
    await update_news(row["id"], status="skipped")
    await save_rejection_feedback(row, reason, user_id)
    await create_replacement_request(user_id, row["id"], current_period())
    context.user_data.pop("rejection_news_id", None)


async def rejection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_authorized_id(q.from_user.id):
        return
    data = q.data or ""

    if data.startswith("skip:"):
        await q.answer()
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if not row:
            await q.answer("Пост не знайдено", show_alert=True)
            raise ApplicationHandlerStop
        context.user_data["rejection_news_id"] = news_id
        await q.edit_message_text(
            f"🚫 <b>Чому відхиляєш пост #{news_id}?</b>\n\n"
            "Це важливо: причина збережеться як твій редакторський сигнал. "
            "Наступні новини будуть перевірятися з урахуванням цих відхилень, а цей слот бот спробує заповнити іншою новиною.",
            parse_mode="HTML",
            reply_markup=_reason_buttons(news_id),
        )
        raise ApplicationHandlerStop

    if data.startswith("reject_reason:"):
        await q.answer()
        _, news_id_raw, code = data.split(":", 2)
        news_id = int(news_id_raw)
        row = await get_news(news_id)
        if not row:
            raise ApplicationHandlerStop
        reason = rejection_reason_label(code)
        await _complete_rejection(context, row, q.from_user.id, reason)
        await q.edit_message_text(
            f"✅ <b>Пост #{news_id} відхилено</b>\n\n"
            f"Причина: <b>{html.escape(reason)}</b>\n\n"
            "🧠 Я запам’ятав цей сигнал. 🔎 Пошук нової новини на заміну поставлено в чергу.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Готові пости", callback_data="queue")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
            ]),
        )
        raise ApplicationHandlerStop

    if data.startswith("reject_custom:"):
        await q.answer()
        news_id = int(data.split(":", 1)[1])
        context.user_data["rejection_news_id"] = news_id
        await q.edit_message_text(
            f"✍️ <b>Причина відхилення поста #{news_id}</b>\n\n"
            "Напиши одним повідомленням, чому ця новина не підходить. Наприклад: "
            "<i>«вже неактуально», «занадто дрібна новина», «не хочу трансферні чутки»</i>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data=f"item:{news_id}")]]),
        )
        raise ApplicationHandlerStop


async def rejection_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_authorized_id(update.effective_user.id):
        return
    news_id = context.user_data.get("rejection_news_id")
    if not news_id:
        return
    reason = (update.message.text or "").strip()
    if not reason:
        return
    row = await get_news(int(news_id))
    if not row:
        context.user_data.pop("rejection_news_id", None)
        raise ApplicationHandlerStop
    await _complete_rejection(context, row, update.effective_user.id, reason)
    await update.message.reply_text(
        f"✅ <b>Пост #{news_id} відхилено</b>\n\n"
        f"Причину запам’ятано: <i>{html.escape(reason[:800])}</i>\n\n"
        "🔎 Бот тепер шукає іншу новину на цей слот.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готові пости", callback_data="queue")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]),
    )
    raise ApplicationHandlerStop


def register_rejection_ui(app: Application) -> None:
    app.add_handler(
        CallbackQueryHandler(rejection_callback, pattern=r"^(skip:|reject_reason:|reject_custom:)"),
        group=-7,
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rejection_text_handler), group=-7)
