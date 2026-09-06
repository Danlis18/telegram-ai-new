from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await update.message.reply_text(
        f"🆔 Твій Telegram ID: <code>{user.id}</code>",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


def register_id_handler(app: Application) -> None:
    app.add_handler(CommandHandler("id", show_id), group=-90)
