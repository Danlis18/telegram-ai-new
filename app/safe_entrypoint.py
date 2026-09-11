import asyncio
import logging

from app import main as app_main
from app.album_support import install_album_support
from app.config import settings
from app.premium_emoji_support import install_premium_emoji_support
from app.source_whitelist import install_source_whitelist
from app.telegram_proxy import (
    assert_proxy_ready,
    check_telegram_proxy,
    install_proxy_status_runtime,
)

log = logging.getLogger("telegram-ai-news.safe-entrypoint")


async def _non_interactive_start() -> None:
    """Connect Telegram reader without ever prompting for phone/code on Railway."""
    # Fail closed: if SOCKS5 is configured but the startup health check failed,
    # the Telegram session is never allowed to fall back to Railway's normal IP.
    assert_proxy_ready()

    await app_main.reader.connect()
    authorized = await app_main.reader.is_user_authorized()
    if not authorized:
        raise EOFError(
            "Telegram session is not authorized. Interactive phone login is disabled on Railway."
        )


async def run() -> None:
    # Verify the dedicated SOCKS5 before the admin/read pipeline starts. This
    # check does not touch the Telegram auth key: it only tunnels TCP to Telegram.
    proxy_state = await check_telegram_proxy(settings)
    log.info(
        "Telegram proxy startup status=%s configured=%s endpoint=%s",
        proxy_state.get("status"),
        proxy_state.get("configured"),
        proxy_state.get("endpoint") or "-",
    )

    # Add the proxy state to SPORTS NEWS CONTROL startup messages and ⚙️ status.
    install_proxy_status_runtime(app_main)

    # Teach the rewriter to understand/reuse valid Telegram Premium emoji from
    # manual editor examples before any news is processed.
    install_premium_emoji_support()

    # Install media-album support before the admin bot and reader start.
    install_album_support()

    # Keep the persistent DB and runtime parser restricted to the selected
    # Telegram source whitelist only.
    install_source_whitelist()

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
