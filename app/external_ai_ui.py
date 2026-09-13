import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from app.auth import is_authorized_id
from app.database import get_setting, set_setting
from app.tenant import user_scope
from app.user_ai_runtime import api_key_status, delete_user_api_key, set_user_api_key


def _allowed(update: Update) -> bool:
    return bool(update.effective_user and is_authorized_id(update.effective_user.id))


async def _state(user_id: int) -> dict:
    with user_scope(user_id):
        web = (await get_setting("web_sources_enabled", "true") or "true").lower() == "true"
        matches = (await get_setting("daily_fixtures_enabled", "true") or "true").lower() == "true"
        min_score = int(await get_setting("web_min_score", "62") or 62)
        max_daily = int(await get_setting("web_max_per_day", "8") or 8)
    key = await api_key_status(user_id)
    return {"web": web, "matches": matches, "min_score": min_score, "max_daily": max_daily, "key": key}


def _markup(state: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🌐 Web-новини: {'ON' if state['web'] else 'OFF'}",
            callback_data="ext_toggle_web",
        )],
        [InlineKeyboardButton(
            f"📅 Топ-матчі: {'ON' if state['matches'] else 'OFF'}",
            callback_data="ext_toggle_matches",
        )],
        [
            InlineKeyboardButton("− WebRank", callback_data="ext_score_down"),
            InlineKeyboardButton(f"{state['min_score']}", callback_data="ext_noop"),
            InlineKeyboardButton("+ WebRank", callback_data="ext_score_up"),
        ],
        [
            InlineKeyboardButton("− Ліміт/день", callback_data="ext_daily_down"),
            InlineKeyboardButton(f"{state['max_daily']}", callback_data="ext_noop"),
            InlineKeyboardButton("+ Ліміт/день", callback_data="ext_daily_up"),
        ],
        [InlineKeyboardButton("🔑 Встановити OpenAI API key", callback_data="ext_api_set")],
        [InlineKeyboardButton("🗑 Видалити власний API key", callback_data="ext_api_delete")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
    ])


async def _text(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    state = await _state(user_id)
    key_label = "власний" if state["key"]["custom"] else "спільний Railway"
    text = (
        "🌐 <b>Зовнішні джерела / AI</b>\n\n"
        f"Web-новини: <b>{'ON' if state['web'] else 'OFF'}</b>\n"
        f"Щоденні топ-матчі: <b>{'ON' if state['matches'] else 'OFF'}</b>\n"
        f"Мінімальний WebRank: <b>{state['min_score']}</b>\n"
        f"Макс. web-постів/день: <b>{state['max_daily']}</b>\n\n"
        f"OpenAI: <b>{key_label}</b> · <code>{html.escape(state['key']['masked'])}</code>\n\n"
        "Web-пост без фото/відео не потрапляє в чергу: система спочатку бере релевантне зображення з джерела або генерує власний SPORTS NEWS creative з логотипом."
    )
    return text, _markup(state)


async def show_external_ai(query, user_id: int) -> None:
    text, markup = await _text(user_id)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    q = update.callback_query
    uid = int(update.effective_user.id)
    await q.answer()
    data = q.data or ""

    if data == "external_ai":
        await show_external_ai(q, uid)
        return
    if data == "ext_noop":
        return
    if data == "ext_api_set":
        context.user_data["awaiting_openai_api_key"] = True
        await q.edit_message_text(
            "🔑 <b>Власний OpenAI API key</b>\n\n"
            "Надішли API key наступним повідомленням. Повідомлення з ключем бот одразу видалить, а сам ключ збереже в зашифрованому вигляді окремо для твого workspace.\n\n"
            "Нічого іншого зараз не надсилай.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="external_ai")]]),
        )
        return
    if data == "ext_api_delete":
        await delete_user_api_key(uid)
        await show_external_ai(q, uid)
        return

    with user_scope(uid):
        if data == "ext_toggle_web":
            current = (await get_setting("web_sources_enabled", "true") or "true").lower() == "true"
            await set_setting("web_sources_enabled", "false" if current else "true")
        elif data == "ext_toggle_matches":
            current = (await get_setting("daily_fixtures_enabled", "true") or "true").lower() == "true"
            await set_setting("daily_fixtures_enabled", "false" if current else "true")
        elif data in {"ext_score_up", "ext_score_down"}:
            current = int(await get_setting("web_min_score", "62") or 62)
            current = max(40, min(95, current + (3 if data.endswith("up") else -3)))
            await set_setting("web_min_score", str(current))
        elif data in {"ext_daily_up", "ext_daily_down"}:
            current = int(await get_setting("web_max_per_day", "8") or 8)
            current = max(1, min(20, current + (1 if data.endswith("up") else -1)))
            await set_setting("web_max_per_day", str(current))
    await show_external_ai(q, uid)


async def api_key_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not context.user_data.get("awaiting_openai_api_key"):
        return
    context.user_data.pop("awaiting_openai_api_key", None)
    uid = int(update.effective_user.id)
    raw = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        await set_user_api_key(uid, raw, validate=True)
        text, markup = await _text(uid)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ <b>Власний OpenAI API key підключено</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as exc:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔴 <b>API key не збережено</b>\n\n<code>{html.escape(str(exc)[:500])}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Спробувати ще раз", callback_data="ext_api_set"), InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
        )
    raise ApplicationHandlerStop


def register_external_ai_ui(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^(external_ai|ext_.*)$"), group=-40)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_message), group=-30)
