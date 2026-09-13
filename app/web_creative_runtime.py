import logging
from io import BytesIO

from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.web-creative")


def install_web_creative_runtime() -> None:
    from app import publishing

    if getattr(publishing, "_web_creative_runtime_installed", False):
        return

    original = publishing.process_ready_automation

    async def process_with_web_creative(bot, news_id: int, user_id: int | None = None) -> dict:
        from app.ai_editor import generate_news_image
        from app.database import get_news, update_news
        from app.external_settings import get_external_settings
        from app.config import settings

        probe = None
        if user_id is not None:
            with user_scope(int(user_id)):
                probe = await get_news(int(news_id))
        else:
            probe = await get_news(int(news_id))
            user_id = int((probe or {}).get("user_id") or settings.admin_user_id or 0)

        if probe and str(probe.get("source") or "").startswith("web:") and not probe.get("media_file_id") and user_id:
            cfg = await get_external_settings(int(user_id))
            if cfg.get("generate_creative"):
                try:
                    with user_scope(int(user_id)):
                        image_bytes = await generate_news_image(
                            probe.get("rewritten_text") or probe.get("original_text") or ""
                        )
                    upload = BytesIO(image_bytes)
                    upload.name = f"sports_news_web_{int(news_id)}.jpg"
                    sent = await bot.send_photo(
                        int(user_id),
                        photo=upload,
                        caption=(
                            f"🎨 <b>Креатив для web-поста #{int(news_id)}</b>\n"
                            "Згенеровано автоматично, бо джерело не мало придатного медіа."
                        ),
                        parse_mode="HTML",
                    )
                    file_id = sent.photo[-1].file_id
                    with user_scope(int(user_id)):
                        await update_news(
                            int(news_id),
                            media_type="photo",
                            media_file_id=file_id,
                            original_media_file_id=file_id,
                        )
                    log.info("Generated branded creative for web news_id=%s user_id=%s", news_id, user_id)
                except Exception as exc:
                    # A text story is still valuable; image generation failure must not
                    # kill the candidate or change the established publish workflow.
                    log.exception("Web creative generation failed news_id=%s user_id=%s", news_id, user_id)
                    try:
                        await bot.send_message(
                            int(user_id),
                            f"⚠️ Креатив для web-поста #{int(news_id)} не згенерувався: <code>{type(exc).__name__}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

        return await original(bot, news_id, user_id)

    publishing.process_ready_automation = process_with_web_creative
    publishing._web_creative_runtime_installed = True
    log.info("Installed automatic branded creatives for web posts without media")
