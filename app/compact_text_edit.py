import logging
import re
from contextlib import suppress

from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger("telegram-ai-news.compact-text-edit")


def install_compact_text_edit() -> None:
    """Keep manual text editing in one control message and preserve Premium emoji."""
    from app import admin_bot
    from app.compact_chat_runtime import _editor_message_html

    if getattr(admin_bot, "_compact_text_edit_installed", False):
        return

    original_callback = admin_bot.callback

    async def compact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = (q.data or "") if q else ""
        if q and data.startswith("edit:"):
            # Remember the exact control message that becomes the edit prompt.
            # The submitted editor message will be deleted and this same message
            # will be changed back into the final post, so the chat gets no duplicate.
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
            with suppress(Exception):
                await update.message.delete()
            return

        # Explicitly rebuild Telegram HTML from entities. This is mandatory for
        # custom_emoji: text/text_html alone can lose the custom_emoji_id.
        rendered = _editor_message_html(update.message)
        corrected = admin_bot._clean_editor_text(rendered)
        if len(re.sub(r"<[^>]+>", "", corrected).strip()) < 20:
            # Keep the user's text visible on validation errors so they can fix it.
            return

        old_ai = row.get("rewritten_text") or ""
        await admin_bot.save_editorial_feedback(
            int(news_id), row.get("original_text") or "", old_ai, corrected
        )
        await admin_bot.update_news(int(news_id), rewritten_text=corrected, status="ready")
        row = await admin_bot.get_news(int(news_id))

        prompt_message_id = context.user_data.pop("editing_prompt_message_id", None)
        prompt_chat_id = context.user_data.pop("editing_prompt_chat_id", None)
        context.user_data.pop("editing_news_id", None)

        # Remove the submitted copy after it has been safely stored. The final
        # version stays only once: inside the existing control message.
        with suppress(Exception):
            await update.message.delete()

        final_html = admin_bot.post_html(corrected)
        if prompt_message_id and prompt_chat_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=int(prompt_chat_id),
                    message_id=int(prompt_message_id),
                    text=final_html,
                    parse_mode="HTML",
                    reply_markup=admin_bot.item_menu(row),
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                log.exception("Could not reuse editor control message for post=%s", news_id)

        # Rare fallback for an old/deleted prompt. Still send only the final post,
        # without extra explanatory/duplicate text.
        await context.bot.send_message(
            chat_id=int(update.effective_chat.id),
            text=final_html,
            parse_mode="HTML",
            reply_markup=admin_bot.item_menu(row),
            disable_web_page_preview=True,
        )

    admin_bot.callback = compact_callback
    admin_bot.handle_editor_text = compact_editor_text
    admin_bot._compact_text_edit_installed = True
    log.info("Installed no-duplicate manual text editor with strict Premium emoji entity preservation")
