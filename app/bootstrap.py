from telegram.ext import Application

from app.auth import is_authorized_id
from app.multiuser_ui import main_menu, register_multiuser_ui, shared_guard
from app.tenant import register_tenant_context


def install_application(app: Application) -> None:
    if getattr(app, "_sports_news_multiuser_installed", False):
        return

    register_tenant_context(app)
    register_multiuser_ui(app)

    # Existing admin-bot handlers keep working, but authorization and menu become
    # workspace-aware without duplicating the large admin module.
    try:
        from app import admin_bot

        admin_bot.guard = shared_guard
        admin_bot.main_menu = main_menu
    except Exception:
        pass

    # AI/photo settings are per-user because database settings are automatically
    # namespaced by the current Telegram user context.
    try:
        from app import settings_ui

        settings_ui._is_admin = lambda update: bool(
            update.effective_user and is_authorized_id(update.effective_user.id)
        )
        settings_ui.register_settings_ui(app)
    except Exception:
        pass

    # Allow authorized workspace users to use manual/auto publication controls.
    try:
        from app import publish_ui

        publish_ui._is_admin = lambda update: bool(
            update.effective_user and is_authorized_id(update.effective_user.id)
        )
    except Exception:
        pass

    app._sports_news_multiuser_installed = True
