import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.ai_editor import rewrite_news
from app.config import settings
from app.database import (
    get_news,
    get_queue,
    get_recent,
    get_setting,
    set_setting,
    stats,
    update_news,
)
from app.sources import SOURCES


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Черга новин", callback_data="queue"), InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Керування", callback_data="control"), InlineKeyboardButton("📡 Джерела", callback_data="sources")],
        [InlineKeyboardButton("🕘 Останні", callback_data="recent"), InlineKeyboardButton("ℹ️ Допомога", callback_data="help")],
    ])


def item_menu(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{news_id}"), InlineKeyboardButton("🔄 Перегенерувати", callback_data=f"regen:{news_id}")],
        [InlineKeyboardButton("👁 Оригінал", callback_data=f"original:{news_id}"), InlineKeyboardButton("⏭ Пропустити", callback_data=f"skip:{news_id}")],
        [InlineKeyboardButton("⬅️ До черги", callback_data="queue"), InlineKeyboardButton("🏠 Меню", callback_data="menu")],
    ])


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and settings.admin_user_id and user.id == settings.admin_user_id)


async def guard(update: Update) -> bool:
    if is_admin(update):
        return True
    if update.message and update.message.text == "/id":
        return True
    if update.callback_query:
        await update.callback_query.answer("Немає доступу", show_alert=True)
    elif update.message:
        await update.message.reply_text("🔒 Цей бот працює лише для адміністратора.\n\nЩоб дізнатися свій Telegram ID, надішли /id")
    return False


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram user ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "🤖 <b>AI NEWS CONTROL</b>\n\n"
        "Професійна панель керування автопостингом.\n"
        "Тут можна переглядати чергу, редагувати рішення AI, публікувати новини та контролювати роботу системи.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def show_queue(query):
    rows = await get_queue(15)
    if not rows:
        await query.edit_message_text("📰 <b>Черга порожня</b>\n\nНові відібрані AI новини з'являться тут автоматично.", parse_mode="HTML", reply_markup=main_menu())
        return
    buttons = []
    for row in rows:
        title = (row["rewritten_text"] or row["original_text"] or "Новина").replace("\n", " ")[:45]
        buttons.append([InlineKeyboardButton(f"{row['score']}% · {title}", callback_data=f"item:{row['id']}")])
    buttons.append([InlineKeyboardButton("🔄 Оновити", callback_data="queue"), InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    await query.edit_message_text(
        f"📰 <b>Черга новин</b>\n\nГотово до перевірки: <b>{len(rows)}</b>\nНатисни на новину для перегляду та дій.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_item(query, news_id: int):
    row = await get_news(news_id)
    if not row:
        await query.answer("Новину не знайдено", show_alert=True)
        return
    text = html.escape((row.get("rewritten_text") or "—")[:3000])
    source = html.escape(str(row.get("source", "—")))
    await query.edit_message_text(
        f"🗞 <b>Новина #{row['id']}</b>\n"
        f"📡 Джерело: @{source}\n"
        f"🧠 AI score: <b>{row.get('score', 0)}%</b>\n"
        f"📌 Статус: <code>{row.get('status')}</code>\n\n"
        f"{text}",
        parse_mode="HTML",
        reply_markup=item_menu(news_id),
        disable_web_page_preview=True,
    )


async def show_stats(query):
    s = await stats()
    await query.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"📥 Отримано всього: <b>{s['total']}</b>\n"
        f"🗓 Сьогодні: <b>{s['today']}</b>\n"
        f"📰 У черзі: <b>{s['ready']}</b>\n"
        f"✅ Опубліковано: <b>{s['published']}</b>\n"
        f"🚫 Відхилено AI: <b>{s['rejected']}</b>\n"
        f"⏭ Пропущено вручну: <b>{s['skipped']}</b>\n"
        f"🧠 Середній score: <b>{s['avg_score']}%</b>\n\n"
        f"📡 Активних джерел: <b>{len(SOURCES)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="stats"), InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def show_control(query):
    paused = (await get_setting("processing_paused", "false")) == "true"
    state = "⏸ ПРИЗУПИНЕНО" if paused else "🟢 ПРАЦЮЄ"
    toggle = "▶️ Відновити обробку" if paused else "⏸ Призупинити обробку"
    await query.edit_message_text(
        "⚙️ <b>Керування системою</b>\n\n"
        f"Reader: <b>{state}</b>\n"
        f"Автопублікація: <b>{'ON' if settings.auto_publish else 'OFF'}</b>\n"
        f"Мінімальний AI score: <b>{settings.min_publish_score}%</b>\n\n"
        "Призупинення зупиняє обробку нових повідомлень, але сам сервіс Railway залишається онлайн.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data="toggle_pause")],
            [InlineKeyboardButton("📰 Черга", callback_data="queue"), InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]),
    )


async def show_sources(query):
    preview = "\n".join(f"• @{s}" for s in SOURCES[:35])
    more = len(SOURCES) - 35
    await query.edit_message_text(
        f"📡 <b>Джерела</b>\n\nВсього підключено: <b>{len(SOURCES)}</b>\n\n{html.escape(preview)}"
        + (f"\n\n…і ще <b>{more}</b> каналів" if more > 0 else ""),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def show_recent(query):
    rows = await get_recent(12)
    if not rows:
        await query.edit_message_text("🕘 Історія поки порожня.", reply_markup=main_menu())
        return
    lines = []
    for r in rows:
        icon = {"published": "✅", "ready": "📰", "rejected": "🚫", "skipped": "⏭"}.get(r["status"], "•")
        lines.append(f"{icon} #{r['id']} · {r['score']}% · @{html.escape(str(r['source']))}")
    await query.edit_message_text(
        "🕘 <b>Останні події</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="recent"), InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu":
        await q.edit_message_text("🤖 <b>AI NEWS CONTROL</b>\n\nОбери потрібний розділ:", parse_mode="HTML", reply_markup=main_menu())
    elif data == "queue":
        await show_queue(q)
    elif data == "stats":
        await show_stats(q)
    elif data == "control":
        await show_control(q)
    elif data == "sources":
        await show_sources(q)
    elif data == "recent":
        await show_recent(q)
    elif data == "help":
        await q.edit_message_text(
            "ℹ️ <b>Можливості</b>\n\n"
            "📰 Черга — усі відібрані AI новини.\n"
            "✅ Опублікувати — відправити готовий пост у канал.\n"
            "🔄 Перегенерувати — зробити новий варіант тексту.\n"
            "👁 Оригінал — переглянути текст джерела.\n"
            "⏭ Пропустити — прибрати новину з черги.\n"
            "📊 Статистика — аналітика роботи системи.\n"
            "⚙️ Керування — пауза/відновлення reader.\n"
            "📡 Джерела — список каналів-моніторингу.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    elif data == "toggle_pause":
        paused = (await get_setting("processing_paused", "false")) == "true"
        await set_setting("processing_paused", "false" if paused else "true")
        await show_control(q)
    elif data.startswith("item:"):
        await show_item(q, int(data.split(":", 1)[1]))
    elif data.startswith("original:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row:
            original = html.escape((row.get("original_text") or "—")[:3000])
            await q.edit_message_text(
                f"👁 <b>Оригінал #{news_id}</b>\n\n{original}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"item:{news_id}")]]),
            )
    elif data.startswith("skip:"):
        news_id = int(data.split(":", 1)[1])
        await update_news(news_id, status="skipped")
        await q.answer("Пропущено")
        await show_queue(q)
    elif data.startswith("regen:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row:
            await q.edit_message_text("🧠 Перегенеровую текст…")
            result = await rewrite_news(row["original_text"], row["source"])
            rewritten = (result.get("text") or "").strip()
            score = int(result.get("score", row.get("score", 0)))
            await update_news(news_id, rewritten_text=rewritten, score=score, status="ready")
            await show_item(q, news_id)
    elif data.startswith("publish:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row and row.get("rewritten_text"):
            await context.bot.send_message(settings.target_channel, row["rewritten_text"], disable_web_page_preview=True)
            await update_news(news_id, status="published")
            await q.answer("Опубліковано ✅", show_alert=True)
            await show_queue(q)


async def start_admin_bot() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(callback))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.bot.set_my_commands([
        ("start", "Відкрити панель керування"),
        ("menu", "Головне меню"),
        ("id", "Показати мій Telegram ID"),
    ])
    return app


async def stop_admin_bot(app: Application):
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
