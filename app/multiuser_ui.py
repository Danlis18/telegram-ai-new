import html
import re
from contextlib import suppress
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from app.ai_editor import generate_news_image
from app.auth import is_authorized_id, is_owner_id
from app.database import (
    add_user,
    add_user_source,
    add_user_target,
    count_user_sources,
    delete_user_source,
    delete_user_target,
    get_generation_stats,
    get_news,
    get_user,
    get_default_target,
    list_user_sources,
    list_user_targets,
    list_users,
    set_default_target,
    set_user_active,
    stats,
    update_news,
)
from app.tenant import get_current_user_id


def main_menu() -> InlineKeyboardMarkup:
    uid = get_current_user_id()
    rows = [
        [InlineKeyboardButton("✅ Готові пости", callback_data="queue")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"), InlineKeyboardButton("📦 Архів", callback_data="archive")],
        [InlineKeyboardButton("⚙️ Керування", callback_data="control"), InlineKeyboardButton("📺 Мої канали", callback_data="channels_menu")],
        [InlineKeyboardButton("🧠 AI / Фото / Шаблони", callback_data="ai_settings")],
    ]
    if is_owner_id(uid):
        rows.append([InlineKeyboardButton("👥 Користувачі", callback_data="users_menu")])
    rows.append([InlineKeyboardButton("ℹ️ Як це працює", callback_data="help")])
    return InlineKeyboardMarkup(rows)


async def shared_guard(update: Update) -> bool:
    user = update.effective_user
    if user and is_authorized_id(user.id):
        return True
    if update.message and update.message.text == "/id":
        return True
    if update.callback_query:
        await update.callback_query.answer("Немає доступу", show_alert=True)
    elif update.message:
        await update.message.reply_text(
            "🔒 Доступ до бота надає власник.\n\n"
            "Щоб передати йому свій Telegram ID, надішли /id."
        )
    return False


def _normalize_source(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I)
    value = re.sub(r"^telegram\.me/", "", value, flags=re.I)
    value = value.split("?", 1)[0].strip("/ ").lstrip("@").lower()
    if value.startswith("+") or "/" in value or not re.fullmatch(r"[a-zA-Z0-9_]{4,64}", value):
        raise ValueError("Потрібен публічний @username Telegram-каналу, наприклад @sportsru")
    return value


async def _show_users(query) -> None:
    if not is_owner_id(query.from_user.id):
        await query.answer("Тільки для власника", show_alert=True)
        raise ApplicationHandlerStop
    users = await list_users(False, 50)
    buttons = [[InlineKeyboardButton("➕ Додати користувача", callback_data="user_add")]]
    for row in users:
        uid = int(row["telegram_user_id"])
        owner = row.get("role") == "owner"
        state = "🟢" if row.get("is_active") else "🔴"
        name = (row.get("display_name") or str(uid))[:28]
        label = f"👑 {name}" if owner else f"{state} {name} · {uid}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"user_view:{uid}")])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    await query.edit_message_text(
        "👥 <b>Користувачі SPORTS NEWS</b>\n\n"
        "Власник додає людей за Telegram ID. Кожен користувач отримує окремі джерела, "
        "канали публікації, промти, логотип, шаблони, статистику та чергу постів.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_user(query, user_id: int) -> None:
    if not is_owner_id(query.from_user.id):
        await query.answer("Тільки для власника", show_alert=True)
        raise ApplicationHandlerStop
    row = await get_user(user_id)
    if not row:
        await query.answer("Користувача не знайдено", show_alert=True)
        raise ApplicationHandlerStop
    sources = await list_user_sources(user_id)
    targets = await list_user_targets(user_id)
    active = bool(row.get("is_active"))
    owner = row.get("role") == "owner"
    buttons = []
    if not owner:
        buttons.append([InlineKeyboardButton(
            "🔴 Вимкнути доступ" if active else "🟢 Увімкнути доступ",
            callback_data=f"user_toggle:{user_id}",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Користувачі", callback_data="users_menu")])
    await query.edit_message_text(
        f"👤 <b>{html.escape(row.get('display_name') or str(user_id))}</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Роль: <b>{'OWNER' if owner else 'USER'}</b>\n"
        f"Доступ: <b>{'ON' if active else 'OFF'}</b>\n"
        f"📡 Джерел: <b>{len(sources)}</b>\n"
        f"📺 Каналів публікації: <b>{len(targets)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_channels(query) -> None:
    uid = query.from_user.id
    targets = await list_user_targets(uid)
    sources = await list_user_sources(uid)
    buttons = [
        [InlineKeyboardButton("➕ Додати канал публікації", callback_data="target_add")],
        [InlineKeyboardButton("➕ Додати канал-джерело", callback_data="source_add")],
    ]
    for row in targets[:10]:
        marker = "✅" if row.get("is_default") else "▫️"
        title = (row.get("title") or row.get("channel_ref") or "Канал")[:28]
        buttons.append([
            InlineKeyboardButton(f"{marker} 📺 {title}", callback_data=f"target_select:{row['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"target_delete:{row['id']}"),
        ])
    for row in sources[:15]:
        buttons.append([
            InlineKeyboardButton(f"📡 @{row['username']}", callback_data="noop"),
            InlineKeyboardButton("🗑", callback_data=f"source_delete:{row['id']}"),
        ])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    default_target = await get_default_target(uid)
    await query.edit_message_text(
        "📺 <b>Мій робочий простір</b>\n\n"
        f"Каналів для публікації: <b>{len(targets)}</b>\n"
        f"Джерел контенту: <b>{len(sources)}</b>\n"
        f"Активний канал: <b>{html.escape((default_target or {}).get('title') or (default_target or {}).get('channel_ref') or 'не вибрано')}</b>\n\n"
        "Для каналу публікації спочатку додай цього бота адміністратором каналу. "
        "Нові публічні джерела reader підхоплює автоматично протягом кількох хвилин.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_stats(query) -> None:
    s = await stats()
    source_count = await count_user_sources()
    targets = await list_user_targets()
    gs = await get_generation_stats()
    await query.edit_message_text(
        "📊 <b>Моя статистика</b>\n\n"
        f"📡 Джерел: <b>{source_count}</b>\n"
        f"📺 Каналів публікації: <b>{len(targets)}</b>\n"
        f"📥 Всього новин: <b>{s.get('total', 0)}</b>\n"
        f"📥 Сьогодні: <b>{s.get('today', 0)}</b>\n"
        f"✅ Готово: <b>{s.get('ready', 0)}</b>\n"
        f"📅 Заплановано: <b>{s.get('scheduled', 0)}</b>\n"
        f"📤 Опубліковано: <b>{s.get('published', 0)}</b>\n"
        f"🚫 Відхилено AI: <b>{s.get('rejected', 0)}</b>\n"
        f"🔴 AI-помилки: <b>{s.get('ai_error', 0)}</b>\n\n"
        "🧠 <b>Генерації</b>\n"
        f"✍️ Текст: <b>{gs.get('text_generations', 0)}</b>\n"
        f"🖼 Photo edit: <b>{gs.get('image_edits', 0)}</b>\n"
        f"🎨 Photo generate: <b>{gs.get('image_generations', 0)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def _manual_image_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, news_id: int) -> None:
    q = update.callback_query
    row = await get_news(news_id)
    if not row or row.get("media_type") != "photo":
        await q.answer("Фото не знайдено", show_alert=True)
        raise ApplicationHandlerStop
    file_id = row.get("original_media_file_id") or row.get("media_file_id")
    if not file_id:
        await q.answer("Немає оригінального фото", show_alert=True)
        raise ApplicationHandlerStop

    await q.answer("Редагую фото 🧹")
    status = await context.bot.send_message(q.from_user.id, f"🧹 <b>Редагую фото #{news_id}</b>…", parse_mode="HTML")
    try:
        tg_file = await context.bot.get_file(file_id)
        source = bytes(await tg_file.download_as_bytearray())
        if not source:
            raise RuntimeError("Telegram повернув порожній файл")
        result = await generate_news_image(
            row.get("rewritten_text") or row.get("original_text") or "",
            source_image=source,
        )
        buf = BytesIO(result)
        buf.name = f"sports_news_{news_id}.jpg"
        sent = await context.bot.send_photo(
            q.from_user.id,
            photo=buf,
            caption=f"✅ <b>Фото #{news_id} відредаговано</b>",
            parse_mode="HTML",
        )
        await update_news(news_id, media_file_id=sent.photo[-1].file_id)
        with suppress(Exception):
            await status.delete()
        from app import admin_bot
        updated = await get_news(news_id)
        await q.edit_message_text(
            f"✅ <b>Фото оновлено для поста #{news_id}</b>\n\nПри публікації використається цей варіант.",
            parse_mode="HTML",
            reply_markup=admin_bot.item_menu(updated or row),
        )
    except Exception as exc:
        await status.edit_text(
            f"🔴 <b>Не вдалося відредагувати фото #{news_id}</b>\n\n"
            f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc)[:1200])}</code>",
            parse_mode="HTML",
        )
    raise ApplicationHandlerStop


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_authorized_id(q.from_user.id):
        return
    data = q.data or ""
    await q.answer()

    if data == "noop":
        raise ApplicationHandlerStop
    if data == "users_menu":
        await _show_users(q)
        raise ApplicationHandlerStop
    if data == "user_add":
        if not is_owner_id(q.from_user.id):
            await q.answer("Тільки для власника", show_alert=True)
            raise ApplicationHandlerStop
        context.user_data["account_input"] = "add_user"
        await q.edit_message_text(
            "➕ <b>Додати користувача</b>\n\n"
            "Надішли Telegram ID користувача. Можна одразу додати ім'я після ID:\n"
            "<code>123456789 Іван</code>\n\n"
            "Користувач може дізнатися ID командою /id.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="users_menu")]]),
        )
        raise ApplicationHandlerStop
    if data.startswith("user_view:"):
        await _show_user(q, int(data.split(":", 1)[1]))
        raise ApplicationHandlerStop
    if data.startswith("user_toggle:"):
        if not is_owner_id(q.from_user.id):
            raise ApplicationHandlerStop
        uid = int(data.split(":", 1)[1])
        row = await get_user(uid)
        if row:
            await set_user_active(uid, not bool(row.get("is_active")))
        await _show_user(q, uid)
        raise ApplicationHandlerStop

    if data in {"channels_menu", "sources"}:
        context.user_data.pop("account_input", None)
        await _show_channels(q)
        raise ApplicationHandlerStop
    if data == "target_add":
        context.user_data["account_input"] = "add_target"
        await q.edit_message_text(
            "📺 <b>Додати канал публікації</b>\n\n"
            "1. Додай цього Telegram-бота адміністратором свого каналу.\n"
            "2. Надішли <code>@username</code> каналу або його numeric chat ID <code>-100...</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="channels_menu")]]),
        )
        raise ApplicationHandlerStop
    if data == "source_add":
        context.user_data["account_input"] = "add_source"
        await q.edit_message_text(
            "📡 <b>Додати канал-джерело</b>\n\n"
            "Надішли публічний <code>@username</code> або посилання <code>https://t.me/channel</code>.\n"
            "Після додавання reader підхопить джерело автоматично.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="channels_menu")]]),
        )
        raise ApplicationHandlerStop
    if data.startswith("target_select:"):
        await set_default_target(int(data.split(":", 1)[1]))
        await _show_channels(q)
        raise ApplicationHandlerStop
    if data.startswith("target_delete:"):
        await delete_user_target(int(data.split(":", 1)[1]))
        await _show_channels(q)
        raise ApplicationHandlerStop
    if data.startswith("source_delete:"):
        await delete_user_source(int(data.split(":", 1)[1]))
        await _show_channels(q)
        raise ApplicationHandlerStop
    if data == "stats":
        await _show_stats(q)
        raise ApplicationHandlerStop
    if data.startswith("regen_image:"):
        await _manual_image_edit(update, context, int(data.split(":", 1)[1]))


async def account_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_authorized_id(update.effective_user.id):
        return
    mode = context.user_data.get("account_input")
    if not mode:
        return
    text = (update.message.text or "").strip()

    if mode == "add_user":
        if not is_owner_id(update.effective_user.id):
            context.user_data.pop("account_input", None)
            raise ApplicationHandlerStop
        parts = text.split(maxsplit=1)
        if not parts or not re.fullmatch(r"\d{5,15}", parts[0]):
            await update.message.reply_text("❌ Надішли numeric Telegram ID, наприклад: 123456789 Іван")
            raise ApplicationHandlerStop
        uid = int(parts[0])
        name = parts[1].strip() if len(parts) > 1 else f"User {uid}"
        await add_user(uid, name)
        context.user_data.pop("account_input", None)
        await update.message.reply_text(
            f"✅ Користувача <b>{html.escape(name)}</b> додано.\nID: <code>{uid}</code>\n\n"
            "Тепер він може відкрити цього бота та натиснути /start.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 Користувачі", callback_data="users_menu")]]),
        )
        raise ApplicationHandlerStop

    if mode == "add_source":
        try:
            username = _normalize_source(text)
            await add_user_source(update.effective_user.id, username)
            context.user_data.pop("account_input", None)
            await update.message.reply_text(
                f"✅ Джерело <b>@{html.escape(username)}</b> додано. Reader синхронізує його автоматично.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📺 Мої канали", callback_data="channels_menu")]]),
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        raise ApplicationHandlerStop

    if mode == "add_target":
        try:
            ref: str | int = text
            if re.fullmatch(r"-?\d+", text):
                ref = int(text)
            chat = await context.bot.get_chat(ref)
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            status = str(getattr(member, "status", "")).lower()
            if status not in {"administrator", "creator", "owner"}:
                raise ValueError("Бот має бути адміністратором цього каналу")
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
            await add_user_target(update.effective_user.id, str(chat.id), str(title))
            context.user_data.pop("account_input", None)
            await update.message.reply_text(
                f"✅ Канал <b>{html.escape(str(title))}</b> додано для публікації.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📺 Мої канали", callback_data="channels_menu")]]),
            )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ Не вдалося додати канал.\n<code>{html.escape(str(exc)[:900])}</code>\n\n"
                "Перевір, що бот доданий у канал адміністратором.",
                parse_mode="HTML",
            )
        raise ApplicationHandlerStop


def register_multiuser_ui(app: Application) -> None:
    pattern = (
        r"^(users_menu|user_add|user_view:|user_toggle:|channels_menu|sources|target_add|source_add|"
        r"target_select:|target_delete:|source_delete:|stats$|regen_image:|noop$)"
    )
    app.add_handler(CallbackQueryHandler(account_callback, pattern=pattern), group=-20)
    app.add_handler(MessageHandler(filters.ALL, account_input_handler), group=-20)
