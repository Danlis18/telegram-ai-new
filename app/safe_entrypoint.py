import asyncio
import logging

from app import main as app_main
from app.album_edit_fix import install_album_edit_fix
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
from app.user_publisher import (
    initialize_user_publisher,
    install_user_publisher,
    install_user_publisher_status_runtime,
)

log = logging.getLogger("telegram-ai-news.safe-entrypoint")


async def _non_interactive_start() -> None:
    """Connect Telegram reader without ever prompting for phone/code on Railway."""
    assert_proxy_ready()
    await app_main.reader.connect()
    authorized = await app_main.reader.is_user_authorized()
    if not authorized:
        raise EOFError(
            "Telegram session is not authorized. Interactive phone login is disabled on Railway."
        )


async def run() -> None:
    proxy_state = await check_telegram_proxy(settings)
    log.info(
        "Telegram proxy startup status=%s configured=%s endpoint=%s",
        proxy_state.get("status"),
        proxy_state.get("configured"),
        proxy_state.get("endpoint") or "-",
    )

    install_proxy_status_runtime(app_main)
    install_user_publisher_status_runtime(app_main)

    # Premium emoji are preserved from source/editor examples before any news is processed.
    install_premium_emoji_support()

    # Albums and batch photo editing.
    install_album_support()
    install_album_edit_fix()

    # Per-source publication channel routing.
    install_channel_routing()

    # Final publishing layer: if TELEGRAM_PUBLISHER_SESSION_FILE_B64_1/_2 are
    # configured, the Premium Telegram user account publishes through MTProto.
    # Without them, existing Bot API publication remains unchanged.
    install_user_publisher()
    await initialize_user_publisher()

    # Selected default sources plus manually added sources.
    install_source_whitelist()

    from app import admin_bot
    from app.bootstrap import install_application

    album_register_publish_ui = admin_bot.register_publish_ui

    def register_publish_ui_with_workspace(app):
        album_register_publish_ui(app)
        install_application(app)

    admin_bot.register_publish_ui = register_publish_ui_with_workspace

    # Remove old developer-only commands from production bot.
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

    app_main.reader.start = _non_interactive_start
    await app_main.main()


if __name__ == "__main__":
    asyncio.run(run())
