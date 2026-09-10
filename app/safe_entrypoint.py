import asyncio
import logging

from app import main as app_main
from app.album_support import install_album_support
from app.premium_emoji_support import install_premium_emoji_support

log = logging.getLogger("telegram-ai-news.safe-entrypoint")


async def _non_interactive_start() -> None:
    """Connect Telegram reader without ever prompting for phone/code on Railway."""
    await app_main.reader.connect()
    authorized = await app_main.reader.is_user_authorized()
    if not authorized:
        raise EOFError(
            "Telegram session is not authorized. Interactive phone login is disabled on Railway."
        )


async def run() -> None:
    # Teach the rewriter to understand/reuse valid Telegram Premium emoji from
    # manual editor examples before any news is processed.
    install_premium_emoji_support()

    # Install media-album support before the admin bot and reader start.
    install_album_support()

    # Ensure the existing multi-user/quota/rejection workspace handlers are
    # installed on the Application that admin_bot creates.
    from app import admin_bot
    from app.bootstrap import install_application

    album_register_publish_ui = admin_bot.register_publish_ui

    def register_publish_ui_with_workspace(app):
        album_register_publish_ui(app)
        install_application(app)

    admin_bot.register_publish_ui = register_publish_ui_with_workspace

    # app.main calls reader.start(); replace it with a Railway-safe version that
    # only validates the uploaded session and never asks stdin for a phone/code.
    app_main.reader.start = _non_interactive_start
    await app_main.main()


if __name__ == "__main__":
    asyncio.run(run())
