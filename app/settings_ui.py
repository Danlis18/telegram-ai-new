import base64
import html
import os
from io import BytesIO

from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from app.config import settings
from app.database import (
    delete_custom_template,
    get_custom_template,
    get_generation_stats,
    get_setting,
    list_custom_templates,
    save_custom_template,
    set_setting,
)


PROMPT_KEYS = {
    "prompt_text": ("text_prompt_custom", "✍️ Промт генерації тексту"),
    "prompt_emoji": ("emoji_prompt_custom", "🙂 Правила смайликів"),
    "prompt_image": ("image_prompt_custom", "🖼 Промт генерації/редагування фото"),
}


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and settings.admin_user_id and user.id == settings.admin_user_id)


def _short(value: str | None, limit: int = 170) -> str:
    value = (value or "").strip()
    if not value:
        return "<i>стандартний промт</i>"
    value = value.replace("\n", " ")
    return html.escape(value[:limit] + ("…" if len(value) > limit else ""))


async def show_ai_settings(query) -> None:
    text_prompt = await get_setting("text_prompt_custom", "")
    emoji_prompt = await get_setting("emoji_prompt_custom", "")
    image_prompt = await get_setting("image_prompt_custom", "")
    logo_custom = bool(await get_setting("sports_news_logo_b64", ""))
    template_mode = (await get_setting("template_mode", "off") or "off").lower()
    selected_id = await get_setting("selected_template_id", "")
    templates = await list_custom_templates(50)
    gs = await get_generation_stats()
    volume = bool((os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip())

    await query.edit_message_text(
        "🧠 <b>AI / Фото налаштування</b>\n\n"
        f"✍️ <b>Текст:</b> {_short(text_prompt)}\n"
        f"🙂 <b>Смайлики:</b> {_short(emoji_prompt)}\n"
        f"🖼 <b>Фото:</b> {_short(image_prompt)}\n\n"
        f"🏷 Логотип: <b>{'власний завантажений' if logo_custom else 'вбудований'}</b>\n"
        f"🧩 Шаблони: <b>{'ON' if template_mode == 'on' else 'OFF'}</b> · збережено <b>{len(templates)}</b> · активний <b>{html.escape(selected_id or '—')}</b>\n\n"
        "📊 <b>Накопичувальна статистика генерацій</b>\n"
        f"• Текст: <b>{gs.get('text_generations', 0)}</b>\n"
        f"• Фото edit: <b>{gs.get('image_edits', 0)}</b>\n"
        f"• Фото generate: <b>{gs.get('image_generations', 0)}</b>\n"
        f"• Помилки фото: <b>{gs.get('image_errors', 0)}</b>\n\n"
        f"💾 База: <b>{'Railway Volume ✅' if volume else 'локальний диск ⚠️'}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Промт тексту", callback_data="prompt_text"), InlineKeyboardButton("🙂 Смайлики", callback_data="prompt_emoji")],
            [InlineKeyboardButton("🖼 Промт фото", callback_data="prompt_image")],
            [InlineKeyboardButton("🏷 Оновити логотип", callback_data="upload_logo")],
            [InlineKeyboardButton("🧩 Шаблони", callback_data="templates_menu")],
            [InlineKeyboardButton("⬅️ Керування", callback_data="control")],
        ]),
        disable_web_page_preview=True,
    )


async def _show_templates(query) -> None:
    rows = await list_custom_templates(12)
    mode = (await get_setting("template_mode", "off") or "off").lower()
    selected = await get_setting("selected_template_id", "") or ""
    buttons = [[InlineKeyboardButton(
        f"{'🟢' if mode == 'on' else '⚪️'} Використовувати шаблони: {'ON' if mode == 'on' else 'OFF'}",
        callback_data="toggle_template_mode",
    )]]
    buttons.append([InlineKeyboardButton("➕ Додати PNG-шаблон", callback_data="upload_template")])
    for row in rows:
        marker = "✅" if str(row["id"]) == selected else "▫️"
        buttons.append([InlineKeyboardButton(f"{marker} #{row['id']} · {row['name'][:32]}", callback_data=f"select_template:{row['id']}")])
    if selected:
        buttons.append([InlineKeyboardButton("🗑 Видалити активний шаблон", callback_data="delete_selected_template")])
    buttons.append([InlineKeyboardButton("⬅️ AI / Фото", callback_data="ai_settings")])

    await query.edit_message_text(
        "🧩 <b>Шаблони фото</b>\n\n"
        "Шаблон має бути <b>PNG з прозорою областю</b>, куди вставляється фото. "
        "Бот масштабує фото під розмір шаблону, кладе його знизу, а PNG-дизайн — зверху.\n\n"
        f"Збережено: <b>{len(rows)}</b> · Активний: <b>{html.escape(selected or '—')}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "ai_settings":
        context.user_data.pop("settings_input", None)
        await show_ai_settings(q)
        raise ApplicationHandlerStop

    if data in PROMPT_KEYS:
        key, label = PROMPT_KEYS[data]
        current = await get_setting(key, "")
        context.user_data["settings_input"] = key
        await q.edit_message_text(
            f"{label}\n\n"
            f"<b>Зараз:</b> {_short(current, 700)}\n\n"
            "Надішли новий промт одним повідомленням. Він буде доданий до базових правил.\n"
            "Щоб повернути стандартний варіант — надішли <code>RESET</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="ai_settings")]]),
        )
        raise ApplicationHandlerStop

    if data == "upload_logo":
        context.user_data["settings_input"] = "logo"
        await q.edit_message_text(
            "🏷 <b>Новий логотип SPORTS NEWS</b>\n\n"
            "Надішли логотип як <b>PNG-файл / document</b>. Прозорий фон рекомендований. "
            "Логотип збережеться в базі та переживатиме перезапуски бота.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="ai_settings")]]),
        )
        raise ApplicationHandlerStop

    if data == "templates_menu":
        context.user_data.pop("settings_input", None)
        await _show_templates(q)
        raise ApplicationHandlerStop

    if data == "toggle_template_mode":
        current = (await get_setting("template_mode", "off") or "off").lower()
        await set_setting("template_mode", "off" if current == "on" else "on")
        await _show_templates(q)
        raise ApplicationHandlerStop

    if data == "upload_template":
        context.user_data["settings_input"] = "template"
        await q.edit_message_text(
            "➕ <b>Додати шаблон</b>\n\n"
            "Надішли <b>PNG як файл/document</b>. У PNG повинна бути прозора область для фото. "
            "Назву можеш написати в caption; якщо її немає — використаю ім’я файлу.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="templates_menu")]]),
        )
        raise ApplicationHandlerStop

    if data.startswith("select_template:"):
        template_id = int(data.split(":", 1)[1])
        if await get_custom_template(template_id):
            await set_setting("selected_template_id", str(template_id))
        await _show_templates(q)
        raise ApplicationHandlerStop

    if data == "delete_selected_template":
        selected = await get_setting("selected_template_id", "") or ""
        if selected.isdigit():
            await delete_custom_template(int(selected))
        await set_setting("selected_template_id", "")
        await _show_templates(q)
        raise ApplicationHandlerStop


async def _download_image_bytes(message, context: ContextTypes.DEFAULT_TYPE) -> tuple[bytes, str]:
    if message.document:
        file_id = message.document.file_id
        name = message.document.file_name or "image.png"
    elif message.photo:
        file_id = message.photo[-1].file_id
        name = "telegram_photo.jpg"
    else:
        raise ValueError("Надішли саме фото або PNG-файл")
    tg_file = await context.bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    if not data:
        raise ValueError("Telegram повернув порожній файл")
    return data, name


def _validate_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        image.load()
        return image
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"Файл не є коректним зображенням: {exc}") from exc


async def settings_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    mode = context.user_data.get("settings_input")
    if not mode or not update.message:
        return

    if mode in {"text_prompt_custom", "emoji_prompt_custom", "image_prompt_custom"}:
        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("Надішли промт текстовим повідомленням.")
            raise ApplicationHandlerStop
        await set_setting(mode, "" if text.upper() == "RESET" else text[:8000])
        context.user_data.pop("settings_input", None)
        await update.message.reply_text(
            "✅ Налаштування промта збережено.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 AI / Фото", callback_data="ai_settings")]]),
        )
        raise ApplicationHandlerStop

    if mode == "logo":
        try:
            data, name = await _download_image_bytes(update.message, context)
            image = _validate_image(data)
            if image.width < 64 or image.height < 64:
                raise ValueError("Логотип занадто малий")
            await set_setting("sports_news_logo_b64", base64.b64encode(data).decode("ascii"))
            context.user_data.pop("settings_input", None)
            await update.message.reply_text(
                f"✅ Логотип <b>{html.escape(name)}</b> збережено. Наступні фото використовуватимуть його.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 AI / Фото", callback_data="ai_settings")]]),
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        raise ApplicationHandlerStop

    if mode == "template":
        try:
            data, filename = await _download_image_bytes(update.message, context)
            image = _validate_image(data).convert("RGBA")
            alpha = image.getchannel("A")
            if alpha.getextrema()[0] >= 250:
                raise ValueError("У шаблоні немає прозорої області. Надішли PNG з прозорим місцем під фото.")
            name = (update.message.caption or filename.rsplit(".", 1)[0] or "Template").strip()[:120]
            template_id = await save_custom_template(name, data)
            await set_setting("selected_template_id", str(template_id))
            context.user_data.pop("settings_input", None)
            await update.message.reply_text(
                f"✅ Шаблон <b>#{template_id} · {html.escape(name)}</b> збережено й вибрано активним.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 Шаблони", callback_data="templates_menu")]]),
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        raise ApplicationHandlerStop


def register_settings_ui(app: Application) -> None:
    app.add_handler(
        CallbackQueryHandler(
            settings_callback,
            pattern=r"^(ai_settings|prompt_text|prompt_emoji|prompt_image|upload_logo|templates_menu|toggle_template_mode|upload_template|select_template:|delete_selected_template)",
        ),
        group=-2,
    )
    app.add_handler(MessageHandler(filters.ALL, settings_input_handler), group=-2)
