import logging
import re

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

log = logging.getLogger("telegram-ai-news.compact-text-edit")


def install_compact_text_edit() -> None:
    """Store manual edits once, preserve Premium emoji, and never echo duplicate post text."""
    from app import admin_bot
    from app.compact_chat_runtime import _editor_message_html

    if getattr(admin_bot, "_compact_text_edit_installed", False):
        return

    original_callback = admin_bot.callback

    async def compact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = (q.data or "") if q else ""
        if q and data.startswith("edit:"):
            # This exact bot message becomes a compact status/control block after save.
            context.user_data["editing_prompt_message_id"] = int(q.message.message_id)
            context.user_data["editing_prompt_chat_id"] = int(q.message.chat_id)
        elif data in {"menu", "queue", "cancel_edit"}:
            context.user_data.pop("editing_prompt_message_id", None)
            context.user_data.pop("editing_prompt_chat_id", None)
        return await original_callback(update, context)

    async def compact_editor_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await admin_bot.guard(update):
            return
        news_id = context.user_data.get("editing_news_id")
        if not news_id or not update.message:
            return

        row = await admin_bot.get_news(int(news_id))
        if not row:
            context.user_data.pop("editing_news_id", None)
            context.user_data.pop("editing_prompt_message_id", None)
            context.user_data.pop("editing_prompt_chat_id", None)
            raise ApplicationHandlerStop

        # Rebuild HTML directly from Telegram entities. custom_emoji_id is preserved
        # exactly, so Premium emoji remain Premium in storage and final publication.
        rendered = _editor_message_html(update.message)
        corrected = admin_bot._clean_editor_text(rendered)
        if len(re.sub(r"<[^>]+>", "", corrected).strip()) < 20:
            # Do not create another explanatory text copy. Leave the user's message
            # visible so it can be corrected and resubmitted.
            raise ApplicationHandlerStop

        old_ai = row.get("rewritten_text") or ""
        await admin_bot.save_editorial_feedback(
            int(news_id), row.get("original_text") or "", old_ai, corrected
        )
        await admin_bot.update_news(int(news_id), rewritten_text=corrected, status="ready")
        row = await admin_bot.get_news(int(news_id))

        prompt_message_id = context.user_data.pop("editing_prompt_message_id", None)
        prompt_chat_id = context.user_data.pop("editing_prompt_chat_id", None)
        context.user_data.pop("editing_news_id", None)

        # STRICT NO-DUPLICATE RULE:
        # Never resend/echo the edited post text. The user's submitted Telegram
        # message is the single visible copy (and therefore keeps native Premium
        # emoji rendering). The bot only keeps a compact control/status message.
        if prompt_message_id and prompt_chat_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=int(prompt_chat_id),
                    message_id=int(prompt_message_id),
                    text=(
                        f"✅ <b>Текст поста #{news_id} збережено</b>\n"
                        "Premium emoji та форматування збережені."
                    ),
                    parse_mode="HTML",
                    reply_markup=admin_bot.item_menu(row),
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("Could not update compact editor control for post=%s", news_id)

        # Stop this update completely so no legacy/editor handler can produce a
        # second response for the same submitted text.
        raise ApplicationHandlerStop

    admin_bot.callback = compact_callback
    admin_bot.handle_editor_text = compact_editor_text
    admin_bot._compact_text_edit_installed = True
    log.info("Installed strict single-copy manual editor with Premium emoji preservation")
