import asyncio
import html
import logging

from app import main as app_main
from app.album_edit_fix import install_album_edit_fix
from app.album_support import install_album_support
from app.channel_hygiene import remove_old_test_posts
from app.channel_workspace import install_channel_routing
from app.compact_chat_runtime import install_compact_chat_runtime
from app.compact_text_edit import install_compact_text_edit
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

    me = await app_main.reader.get_me()
    reader_id = int(getattr(me, "id", 0) or 0)
    reader_username = (getattr(me, "username", None) or "").strip()
    reader_name = " ".join(
        part for part in (
            (getattr(me, "first_name", None) or "").strip(),
            (getattr(me, "last_name", None) or "").strip(),
        )
        if part
    )
    app_main.reader_identity = {
        "id": reader_id,
        "username": reader_username,
        "name": reader_name,
    }
    log.info(
        "Reader authorized account id=%s username=%s name=%s",
        reader_id,
        f"@{reader_username}" if reader_username else "(none)",
        reader_name or "-",
    )

    if settings.admin_user_id:
        account_label = f"@{html.escape(reader_username)}" if reader_username else "<i>без @username</i>"
        name_line = f"\nІм’я: <b>{html.escape(reader_name)}</b>" if reader_name else ""
        await app_main.notify_user(
            int(settings.admin_user_id),
            "👤 <b>Telegram Reader Account</b>\n\n"
            f"Акаунт: <b>{account_label}</b>\n"
            f"Telegram ID: <code>{reader_id}</code>"
            f"{name_line}\n\n"
            "Це саме той акаунт, чия .session зараз використовується reader-ом.",
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

    install_premium_emoji_support()
    install_album_support()
    install_album_edit_fix()
    install_compact_chat_runtime()
    install_compact_text_edit()
    install_channel_routing()

    # Premium user publisher is initialized only as the transport for real news.
    # No startup/test/demo/channel message is ever sent automatically.
    install_user_publisher()
    await initialize_user_publisher()

    # User explicitly requested removal of the old Premium/test diagnostics.
    # This only deletes known test posts; it never sends anything to the channel.
    await remove_old_test_posts()

    install_source_whitelist()

    from app import admin_bot
    from app.bootstrap import install_application

    album_register_publish_ui = admin_bot.register_publish_ui

    def register_publish_ui_with_workspace(app):
        album_register_publish_ui(app)
        install_application(app)

    admin_bot.register_publish_ui = register_publish_ui_with_workspace

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
