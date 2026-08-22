import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes

from app.auth import is_authorized_id
from app.content_policy import PERIODS, quota_state, set_period_quota


async def show_quota_settings(query) -> None:
    uid = query.from_user.id
    state = await quota_state(uid)
    lines = ["📊 <b>Ліміти постів протягом доби</b>", ""]
    buttons = []
    for key in ("morning", "day", "evening"):
        item = state[key]
        lines.append(f"{item['label']}: <b>{item['count']}/{item['quota']}</b>")
        buttons.append([
            InlineKeyboardButton("➖", callback_data=f"quota_delta:{key}:-1"),
            InlineKeyboardButton(f"{item['label']} · {item['quota']}", callback_data="quota_noop"),
            InlineKeyboardButton("➕", callback_data=f"quota_delta:{key}:1"),
        ])
    lines.extend([
        "",
        "У період 00:00–06:00 нові пости не подаються.",
        "Якщо ти відхиляєш пост, він звільняє слот і бот шукає іншу новину на заміну.",
    ])
    buttons.extend([
        [InlineKeyboardButton("🔄 Оновити", callback_data="quota_settings")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
    ])
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def quota_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_authorized_id(q.from_user.id):
        return
    data = q.data or ""
    await q.answer()
    if data == "quota_noop":
        raise ApplicationHandlerStop
    if data == "quota_settings":
        await show_quota_settings(q)
        raise ApplicationHandlerStop
    if data.startswith("quota_delta:"):
        _, key, delta_raw = data.split(":", 2)
        if key not in PERIODS:
            raise ApplicationHandlerStop
        state = await quota_state(q.from_user.id)
        current = int(state[key]["quota"])
        await set_period_quota(key, current + int(delta_raw))
        await show_quota_settings(q)
        raise ApplicationHandlerStop


def register_quota_ui(app: Application) -> None:
    app.add_handler(
        CallbackQueryHandler(quota_callback, pattern=r"^(quota_settings|quota_noop|quota_delta:)"),
        group=-6,
    )
