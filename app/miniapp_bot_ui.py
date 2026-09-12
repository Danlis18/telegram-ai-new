import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from app.auth import is_authorized_id
from app.miniapp_server import miniapp_public_url

log = logging.getLogger("telegram-ai-news.miniapp-bot-ui")


def _with_miniapp_button(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    url = miniapp_public_url()
    if not url:
        return markup
    rows = [list(row) for row in markup.inline_keyboard]
    if any(button.web_app for row in rows for button in row):
        return markup
    insert_at = max(0, len(rows) - 1)
    rows.insert(
        insert_at,
        [InlineKeyboardButton("✨ Відкрити Mini App", web_app=WebAppInfo(url=url))],
    )
    return InlineKeyboardMarkup(rows)


async def _cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_authorized_id(user.id):
        return
    url = miniapp_public_url()
    if not url:
        await update.message.reply_text(
            "Mini App уже встановлений у проєкті, але Railway Public Domain ще не доступний."
        )
        return
    await update.message.reply_text(
        "✨ <b>Auto Posting Mini App</b>\n\nПовний центр керування в окремому інтерфейсі. Бот продовжує працювати як і раніше.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Відкрити Mini App", web_app=WebAppInfo(url=url))]]
        ),
    )


def install_miniapp_bot_ui(app: Application) -> None:
    """Add Mini App as an extra option without replacing any existing bot workflow."""
    if getattr(app, "_auto_posting_miniapp_ui", False):
        return
    url = miniapp_public_url()
    if url:
        try:
            from app import admin_bot, bootstrap, multiuser_ui

            for module, attr in (
                (admin_bot, "main_menu"),
                (multiuser_ui, "main_menu"),
                (bootstrap, "enhanced_main_menu"),
            ):
                original = getattr(module, attr, None)
                if not original or getattr(original, "_miniapp_wrapped", False):
                    continue

                def wrapped(_original=original):
                    return _with_miniapp_button(_original())

                wrapped._miniapp_wrapped = True
                setattr(module, attr, wrapped)
        except Exception:
            log.exception("Could not inject Mini App button into bot menus")
    app.add_handler(CommandHandler("app", _cmd_app), group=-40)
    app._auto_posting_miniapp_ui = True
    log.info("Mini App bot UI installed url=%s", url or "not-public-yet")
