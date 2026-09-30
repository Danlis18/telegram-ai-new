import html
import logging

from telegram import MenuButtonWebApp, WebAppInfo

from app import main as app_main
from app.album_edit_fix import install_album_edit_fix
from app.album_support import install_album_support
from app.channel_hygiene import remove_old_test_posts
from app.channel_workspace import install_channel_routing
from app.compact_chat_runtime import install_compact_chat_runtime
from app.compact_text_edit import install_compact_text_edit
from app.config import settings
from app.editorial_policy_runtime import install_editorial_policy
from app.external_control_api import install_external_control_api
from app.match_schedule import start_match_schedule_worker
from app.match_schedule_controls import install_match_schedule_controls
from app.miniapp_bot_ui import install_miniapp_bot_ui
from app.miniapp_channel_enhancements import install_channel_miniapp_enhancements
from app.miniapp_server import miniapp_public_url, start_miniapp_server
from app.persistence_runtime import prepare_persistence, start_persistence_backup_worker, storage_status
from app.runtime_state import mark, track_task
from app.premium_emoji_registry import install_telegram_emoji_learning, learn_from_existing_content
from app.premium_emoji_support import install_premium_emoji_support
from app.source_whitelist import install_source_whitelist
from app.style_punctuation_runtime import install_punctuation_style
from app.telegram_proxy import assert_proxy_ready, check_telegram_proxy, install_proxy_status_runtime
from app.ukrainian_output_runtime import install_ukrainian_output_policy
from app.user_ai_runtime import install_user_ai_runtime
from app.user_publisher import initialize_user_publisher, install_user_publisher, install_user_publisher_status_runtime
from app.web_news_ingest import start_web_news_worker

log = logging.getLogger("telegram-ai-news.runtime")


async def _reader_start_non_interactive() -> None:
    """Connect the parser account on Railway without ever prompting for phone/code."""
    assert_proxy_ready()
    await app_main.reader.connect()
    if not await app_main.reader.is_user_authorized():
        raise EOFError("Telegram session is not authorized. Interactive phone login is disabled on Railway.")

    me = await app_main.reader.get_me()
    reader_id = int(getattr(me, "id", 0) or 0)
    reader_username = (getattr(me, "username", None) or "").strip()
    reader_name = " ".join(
        part
        for part in (
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
            f"Telegram ID: <code>{reader_id}</code>{name_line}\n\n"
            "Це саме той акаунт, чия .session зараз використовується reader-ом.",
        )


async def _prepare_infrastructure() -> None:
    await prepare_persistence()
    storage = storage_status()
    backup_task = start_persistence_backup_worker()
    if backup_task is not None:
        track_task("persistence_backup", backup_task, detail="rolling SQLite snapshots")
    mark(
        "storage",
        "online" if storage.get("persistent") else "warning",
        detail=storage.get("mode") or "ephemeral",
        persistent=bool(storage.get("persistent")),
        database=storage.get("database") or "",
    )
    log.info("Storage status: %s", storage)

    proxy_state = await check_telegram_proxy(settings)
    proxy_status = str(proxy_state.get("status") or "UNKNOWN").upper()
    mark(
        "telegram_proxy",
        "online" if proxy_status == "ONLINE" else "warning",
        detail=proxy_state.get("endpoint") or ("disabled" if not proxy_state.get("configured") else proxy_status),
        configured=bool(proxy_state.get("configured")),
    )
    log.info(
        "Telegram proxy startup status=%s configured=%s endpoint=%s",
        proxy_state.get("status"),
        proxy_state.get("configured"),
        proxy_state.get("endpoint") or "-",
    )


def _install_runtime_layers() -> None:
    """Install additive compatibility layers in one documented, deterministic order."""
    install_proxy_status_runtime(app_main)
    install_user_publisher_status_runtime(app_main)

    # AI routing comes first so every later policy/editor path automatically uses
    # the active workspace's encrypted key, with Railway shared-key fallback.
    install_user_ai_runtime()

    # Content transformation pipeline.
    install_premium_emoji_support()
    install_editorial_policy()
    install_ukrainian_output_policy()
    install_punctuation_style()

    # Media + moderation runtime.
    install_album_support()
    install_album_edit_fix()
    install_compact_chat_runtime()
    install_compact_text_edit()
    install_channel_routing()

    # Source/ranking/UI extensions.
    install_source_whitelist()
    install_match_schedule_controls()
    install_channel_miniapp_enhancements()
    install_external_control_api()


async def _initialize_integrations() -> None:
    install_telegram_emoji_learning(app_main)
    learned = await learn_from_existing_content()
    mark("premium_emoji_registry", "online", detail=f"{int(learned or 0)} learned aliases")

    install_user_publisher()
    publisher = await initialize_user_publisher()
    mark(
        "premium_publisher",
        "online" if publisher.get("online") else "warning",
        detail=(f"@{publisher.get('username')}" if publisher.get("username") else publisher.get("error") or "not configured"),
        premium=bool(publisher.get("premium")),
    )

    # Historical diagnostic posts are cleanup-only; this never sends a startup post.
    await remove_old_test_posts()


async def _start_services() -> None:
    await start_miniapp_server()
    mark("miniapp", "online", detail=miniapp_public_url() or "local")

    # Independent workers. Both respect workspace switches; web discovery also
    # requires usable branded media before a candidate reaches moderation.
    match_task = start_match_schedule_worker()
    web_task = start_web_news_worker()
    if match_task is not None:
        track_task("daily_matches", match_task, detail="top fixtures worker")
    if web_task is not None:
        track_task("web_discovery", web_task, detail="multisource sports discovery")


def _install_admin_bot_runtime() -> None:
    """Attach workspace/Mini App controls without changing the proven admin-bot flow."""
    from app import admin_bot
    from app.bootstrap import install_application
    from app.database import list_users

    original_register_publish_ui = admin_bot.register_publish_ui

    def register_publish_ui_with_workspace(app):
        original_register_publish_ui(app)
        install_application(app)
        install_miniapp_bot_ui(app)

    admin_bot.register_publish_ui = register_publish_ui_with_workspace
    original_start_admin_bot = admin_bot.start_admin_bot

    async def start_admin_bot_with_product_menu():
        app = await original_start_admin_bot()

        commands = [
            ("start", "Відкрити SPORTS NEWS CONTROL"),
            ("menu", "Головне меню"),
        ]
        app_url = miniapp_public_url()
        if app_url:
            commands.append(("app", "Відкрити Mini App"))
        commands.append(("id", "Показати Telegram ID"))
        await app.bot.set_my_commands(commands)

        if app_url:
            menu_button = MenuButtonWebApp(text="OPEN", web_app=WebAppInfo(url=app_url))
            await app.bot.set_chat_menu_button(menu_button=menu_button)

            chat_ids: set[int] = set()
            if settings.admin_user_id:
                chat_ids.add(int(settings.admin_user_id))
            try:
                for row in await list_users(active_only=True):
                    user_id = int(row.get("telegram_user_id") or 0)
                    if user_id:
                        chat_ids.add(user_id)
            except Exception:
                log.exception("Could not enumerate users while configuring Mini App menu buttons")

            for chat_id in chat_ids:
                try:
                    await app.bot.set_chat_menu_button(chat_id=chat_id, menu_button=menu_button)
                except Exception:
                    log.exception("Could not configure Mini App OPEN button for chat_id=%s", chat_id)

            log.info(
                "Mini App OPEN button configured globally and for %d active chats url=%s",
                len(chat_ids),
                app_url,
            )
        return app

    admin_bot.start_admin_bot = start_admin_bot_with_product_menu
    app_main.start_admin_bot = start_admin_bot_with_product_menu


async def run_production() -> None:
    """Single production bootstrap: infrastructure -> runtime -> services -> main loop."""
    await _prepare_infrastructure()
    _install_runtime_layers()
    await _initialize_integrations()
    await _start_services()
    _install_admin_bot_runtime()

    app_main.reader.start = _reader_start_non_interactive
    mark("runtime", "online", detail="production bootstrap complete")
    await app_main.main()
