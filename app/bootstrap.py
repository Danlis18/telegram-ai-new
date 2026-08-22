import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from app.auth import is_authorized_id
from app.content_policy import ensure_policy_schema
from app.multiuser_ui import main_menu as base_main_menu, register_multiuser_ui, shared_guard
from app.quota_ui import register_quota_ui
from app.reader_policy import install_reader_policy
from app.rejection_ui import register_rejection_ui
from app.tenant import register_tenant_context


def enhanced_main_menu() -> InlineKeyboardMarkup:
    markup = base_main_menu()
    rows = [list(row) for row in markup.inline_keyboard]
    insert_at = max(0, len(rows) - 1)
    rows.insert(insert_at, [InlineKeyboardButton("📊 Ліміти постів", callback_data="quota_settings")])
    return InlineKeyboardMarkup(rows)


def install_application(app: Application) -> None:
    if getattr(app, "_sports_news_multiuser_installed", False):
        return

    register_tenant_context(app)
    register_rejection_ui(app)
    register_quota_ui(app)
    register_multiuser_ui(app)
    install_reader_policy(app)

    try:
        asyncio.create_task(ensure_policy_schema())
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
