import html
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.ai_editor import generate_news_image, rewrite_news
from app.config import settings
from app.database import (
    get_archive,
    get_news,
    get_queue,
    get_setting,
    set_setting,
    stats,
    update_news,
)
from app.formatting import post_html
from app.sources import SOURCES


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готові пости", callback_data="queue")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"), InlineKeyboardButton("📦 Архів", callback_data="archive")],
        [InlineKeyboardButton("⚙️ Керування", callback_data="control"), InlineKeyboardButton("📡 Джерела", callback_data="sources")],
        [InlineKeyboardButton("ℹ️ Як це працює", callback_data="help")],
    ])


def item_menu(row: dict) -> InlineKeyboardMarkup:
    news_id = row["id"]
    rows = [
        [InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{news_id}")],
        [
            InlineKeyboardButton("✍️ Інший текст", callback_data=f"regen:{news_id}"),
            InlineKeyboardButton("👁 Оригінал", callback_data=f"original:{news_id}"),
        ],
    ]
    if row.get("media_type") == "photo":
        rows.append([InlineKeyboardButton("🖼 Перегенерувати фото", callback_data=f"regen_image:{news_id}")])
        if row.get("original_media_file_id") and row.get("media_file_id") != row.get("original_media_file_id"):
            rows.append([InlineKeyboardButton("↩️ Повернути оригінальне фото", callback_data=f"restore_image:{news_id}")])
    rows.extend([
        [InlineKeyboardButton("⏭ Пропустити", callback_data=f"skip:{news_id}")],
        [InlineKeyboardButton("⬅️ Готові пости", callback_data="queue"), InlineKeyboardButton("🏠 Меню", callback_data="menu")],
    ])
    return InlineKeyboardMarkup(rows)


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
        "🏟 <b>SPORTS NEWS CONTROL</b>\n\n"
        "Сюди приходять тільки готові новини після повної AI-модерації.\n"
        "Фото показується оригінальне й змінюється лише за твоєю командою. Відео завжди лишається оригінальним.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def show_queue(query):
    rows = await get_queue(20)
    if not rows:
        await query.edit_message_text(
            "✅ <b>Готових постів зараз немає</b>\n\n"
            "Коли новина пройде модерацію, вона автоматично з'явиться тут.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    buttons = []
    for row in rows:
        media = "🖼" if row.get("media_type") == "photo" else "🎥" if row.get("media_type") == "video" else "📝"
        title = (row.get("rewritten_text") or "Готовий пост").replace("\n", " ")[:46]
        buttons.append([InlineKeyboardButton(f"{media} {row['score']}% · {title}", callback_data=f"item:{row['id']}")])
    buttons.append([InlineKeyboardButton("🔄 Оновити", callback_data="queue"), InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    await query.edit_message_text(
        f"✅ <b>Готові пости</b>\n\nНа модерації: <b>{len(rows)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_item(query, news_id: int):
    row = await get_news(news_id)
    if not row:
        await query.answer("Новину не знайдено", show_alert=True)
        return
    source = html.escape(str(row.get("source", "—")))
    media_label = {"photo": "🖼 Фото", "video": "🎥 Відео"}.get(row.get("media_type"), "📝 Без медіа")
    await query.edit_message_text(
        f"✅ <b>Готовий пост #{row['id']}</b>\n"
        f"📡 @{source} · 🧠 <b>{row.get('score', 0)}%</b> · {media_label}\n\n"
        f"{post_html(row.get('rewritten_text') or '')}",
        parse_mode="HTML",
        reply_markup=item_menu(row),
        disable_web_page_preview=True,
    )


async def show_stats(query):
    s = await stats()
    active = await get_setting("active_sources", str(len(SOURCES)))
    await query.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"📡 Активних джерел: <b>{active}/{len(SOURCES)}</b>\n"
        f"📥 Отримано сьогодні: <b>{s['today']}</b>\n"
        f"✅ Готово: <b>{s['ready']}</b>\n"
        f"📤 Опубліковано: <b>{s['published']}</b>\n"
        f"🚫 Відхилено AI: <b>{s['rejected']}</b>\n"
        f"🛑 Відсіяно реклами: <b>{s['advertising']}</b>\n"
        f"🔴 AI-помилки: <b>{s['ai_error']}</b>\n"
        f"🧠 Середній score: <b>{s['avg_score']}%</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="stats"), InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def show_archive(query):
    rows = await get_archive(15)
    if not rows:
        await query.edit_message_text("📦 <b>Архів порожній</b>", parse_mode="HTML", reply_markup=main_menu())
        return
    icons = {"published": "📤", "skipped": "⏭", "rejected": "🚫", "ai_error": "🔴", "raw": "📎", "advertising": "🛑"}
    lines = [f"{icons.get(row['status'], '•')} #{row['id']} · {row['score']}% · @{html.escape(str(row['source']))}" for row in rows]
    await query.edit_message_text(
        "📦 <b>Архів</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="archive"), InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def show_control(query):
    paused = (await get_setting("processing_paused", "false")) == "true"
    state = "⏸ ПРИЗУПИНЕНО" if paused else "🟢 ПРАЦЮЄ"
    toggle = "▶️ Відновити" if paused else "⏸ Призупинити"
    await query.edit_message_text(
        "⚙️ <b>Керування</b>\n\n"
        f"Reader: <b>{state}</b>\n"
        f"Автопублікація: <b>{'ON' if settings.auto_publish else 'OFF'}</b>\n"
        f"Поріг AI: <b>{settings.min_publish_score}%</b>\n\n"
        "У чат надходять тільки повністю готові пости.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data="toggle_pause")],
            [InlineKeyboardButton("✅ Готові пости", callback_data="queue"), InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
        ]),
    )


async def show_sources(query):
    active = await get_setting("active_sources", "0")
    missing_raw = await get_setting("missing_sources", "") or ""
    missing = [x for x in missing_raw.split(",") if x]
    await query.edit_message_text(
        f"📡 <b>Джерела</b>\n\n"
        f"🟢 Активно: <b>{active}/{len(SOURCES)}</b>\n"
        f"🔴 Недоступно: <b>{len(missing)}</b>\n\n"
        "Усі джерела працюють у фоні. Реклама та слабкі пости не потрапляють у готову чергу.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
    )


async def publish_news(context, row: dict):
    text = post_html(row.get("rewritten_text") or "")
    media_type = row.get("media_type")
    file_id = row.get("media_file_id")
    if media_type == "photo" and file_id:
        await context.bot.send_photo(settings.target_channel, photo=file_id, caption=text, parse_mode="HTML")
    elif media_type == "video" and file_id:
        await context.bot.send_video(settings.target_channel, video=file_id, caption=text, parse_mode="HTML", supports_streaming=True)
    else:
        await context.bot.send_message(settings.target_channel, text, parse_mode="HTML", disable_web_page_preview=True)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu":
        await q.edit_message_text("🏟 <b>SPORTS NEWS CONTROL</b>\n\nОбери розділ:", parse_mode="HTML", reply_markup=main_menu())
    elif data == "queue":
        await show_queue(q)
    elif data == "stats":
        await show_stats(q)
    elif data == "archive":
        await show_archive(q)
    elif data == "control":
        await show_control(q)
    elif data == "sources":
        await show_sources(q)
    elif data == "help":
        await q.edit_message_text(
            "ℹ️ <b>Логіка SPORTS NEWS</b>\n\n"
            "1. Reader читає джерела та відсіює рекламу, #реклама, #промо й сторонні лінки.\n"
            "2. Згадки BetKing видаляються повністю, але сама корисна новина може пройти далі.\n"
            "3. AI робить: одне сильне вступне речення + 1-3 короткі абзаци.\n"
            "4. У фіналі код додає клікабельний SPORTS NEWS → на зв’язку.\n"
            "5. Фото береться оригінальне; за бажанням можна перегенерувати.\n"
            "6. Відео ніколи не перегенеровується — публікується оригінал.",
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
            await q.edit_message_text(
                f"👁 <b>Оригінал #{news_id}</b>\n\n{html.escape((row.get('original_text') or '—')[:3000])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"item:{news_id}")]]),
            )
    elif data.startswith("skip:"):
        news_id = int(data.split(":", 1)[1])
        await update_news(news_id, status="skipped")
        await q.answer("Пост пропущено")
        await show_queue(q)
    elif data.startswith("regen:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row:
            await q.edit_message_text("🧠 Готую інший варіант…")
            try:
                result = await rewrite_news(row.get("original_text") or "", row["source"])
                rewritten = (result.get("text") or "").strip()
                score = int(result.get("score", row.get("score", 0)))
                await update_news(news_id, rewritten_text=rewritten, score=score, status="ready")
                await show_item(q, news_id)
            except Exception as exc:
                await q.edit_message_text(
                    f"🔴 <b>Не вдалося перегенерувати</b>\n\n<code>{html.escape(type(exc).__name__)}</code>",
                    parse_mode="HTML",
                    reply_markup=item_menu(row),
                )
    elif data.startswith("regen_image:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row and row.get("media_type") == "photo":
            await q.answer("Генерую нове фото…")
            try:
                image_bytes = await generate_news_image(row.get("rewritten_text") or row.get("original_text") or "")
                buf = BytesIO(image_bytes)
                buf.name = f"sports_news_{news_id}.png"
                sent = await context.bot.send_photo(
                    settings.admin_user_id,
                    photo=buf,
                    caption=f"🖼 <b>Новий варіант фото #{news_id}</b>",
                    parse_mode="HTML",
                )
                file_id = sent.photo[-1].file_id
                await update_news(news_id, media_file_id=file_id)
                row = await get_news(news_id)
                await q.edit_message_text(
                    f"✅ <b>Фото оновлено для поста #{news_id}</b>\n\nТепер при публікації використається цей варіант.",
                    parse_mode="HTML",
                    reply_markup=item_menu(row),
                )
            except Exception as exc:
                await q.answer(f"Помилка генерації: {type(exc).__name__}", show_alert=True)
    elif data.startswith("restore_image:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row and row.get("original_media_file_id"):
            await update_news(news_id, media_file_id=row["original_media_file_id"])
            row = await get_news(news_id)
            await q.edit_message_text(
                f"↩️ <b>Повернено оригінальне фото для поста #{news_id}</b>",
                parse_mode="HTML",
                reply_markup=item_menu(row),
            )
    elif data.startswith("publish:"):
        news_id = int(data.split(":", 1)[1])
        row = await get_news(news_id)
        if row and row.get("rewritten_text"):
            await publish_news(context, row)
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
        ("start", "Відкрити SPORTS NEWS CONTROL"),
        ("menu", "Головне меню"),
        ("id", "Показати Telegram ID"),
    ])
    return app


async def stop_admin_bot(app: Application):
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
