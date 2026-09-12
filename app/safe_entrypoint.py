import asyncio
import logging

from app import main as app_main
from app.album_support import install_album_support
from app.channel_workspace import install_channel_routing
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

    # Route every parsed source to its own publication channel. Existing sources
    # are mapped to the current default channel on first startup.
    install_channel_routing()

    # Seed the selected default source set and keep join/retry logic active.
    # Manually added sources are also allowed and survive future restarts.
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

    # Remove old developer-only commands from both Telegram's command menu and
    # the running handler table. They are no longer part of the production bot.
    original_start_admin_bot = admin_bot.start_admin_bot

    async def start_admin_bot_without_test_commands():
        app = await original_start_admin_bot()
        for group, handlers in list(app.handlers.items()):
            for handler in list(handlers):
                commands = {str(command).lower() for command in (getattr(handler, "commands", None) or set())}
                if commands & {"testimage", "testedit"}:
                    app.remove_handler(handler, group=group)
        await app.bot.set_my_commands([
            ("start", "Відкрити SPORTS NEWS CONTROL"),
            ("menu", "Головне меню"),
            ("id", "Показати Telegram ID"),
        ])
        return app

    admin_bot.start_admin_bot = start_admin_bot_without_test_commands
    app_main.start_admin_bot = start_admin_bot_without_test_commands

    # app.main calls reader.start(); replace it with a Railway-safe version that
    # only validates the uploaded session and never asks stdin for a phone/code.
    app_main.reader.start = _non_interactive_start
    await app_main.main()


if __name__ == "__main__":
    asyncio.run(run())
