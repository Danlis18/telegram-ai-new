import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from app.auth import is_authorized_id, is_owner_id
from app.channel_workspace import ensure_channel_route_schema, register_channel_workspace_ui
from app.channel_workspace_compact import install_compact_channel_page
from app.content_policy import ensure_policy_schema
from app.external_ai_ui import register_external_ai_ui
from app.id_ui import register_id_handler
from app.multiuser_ui import register_multiuser_ui, shared_guard
from app.quota_ui import register_quota_ui
from app.reader_policy import install_reader_policy
from app.rejection_ui import register_rejection_ui
from app.tenant import get_current_user_id, register_tenant_context


def enhanced_main_menu() -> InlineKeyboardMarkup:
    """Compact production menu: daily actions first, configuration second."""
    uid = get_current_user_id()
    rows = [
        [
            InlineKeyboardButton("📰 Черга", callback_data="queue"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("📺 Канали", callback_data="channels_menu"),
            InlineKeyboardButton("⚙️ Система", callback_data="control"),
        ],
        [
            InlineKeyboardButton("🌐 Web / AI", callback_data="external_ai"),
            InlineKeyboardButton("🎨 AI / Фото", callback_data="ai_settings"),
        ],
        [
            InlineKeyboardButton("📦 Архів", callback_data="archive"),
            InlineKeyboardButton("⏱ Ліміти", callback_data="quota_settings"),
        ],
    ]
    if is_owner_id(uid):
        rows.append([InlineKeyboardButton("👥 Користувачі", callback_data="users_menu")])
    rows.append([InlineKeyboardButton("ℹ️ Довідка", callback_data="help")])
    return InlineKeyboardMarkup(rows)


def install_application(app: Application) -> None:
    if getattr(app, "_sports_news_multiuser_installed", False):
        return

    register_tenant_context(app)
    register_id_handler(app)
    register_rejection_ui(app)
    register_quota_ui(app)
    register_external_ai_ui(app)
    install_compact_channel_page()
    register_channel_workspace_ui(app)
    register_multiuser_ui(app)
    install_reader_policy(app)

    try:
        asyncio.create_task(ensure_policy_schema())
    except RuntimeError:
        pass

    try:
        asyncio.create_task(ensure_channel_route_schema())
    except RuntimeError:
        pass

    try:
        from app import admin_bot
        admin_bot.guard = shared_guard
        admin_bot.main_menu = enhanced_main_menu
    except Exception:
        pass

    try:
        from app import multiuser_ui
        multiuser_ui.main_menu = enhanced_main_menu
    except Exception:
        pass

    try:
        from app import settings_ui
        settings_ui._is_admin = lambda update: bool(
            update.effective_user and is_authorized_id(update.effective_user.id)
        )
    except Exception:
        pass

    try:
        from app import publish_ui
        publish_ui._is_admin = lambda update: bool(
            update.effective_user and is_authorized_id(update.effective_user.id)
        )
    except Exception:
        pass

    app._sports_news_multiuser_installed = True
