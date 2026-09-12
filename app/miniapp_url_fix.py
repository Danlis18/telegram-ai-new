import logging

log = logging.getLogger("telegram-ai-news.miniapp-url")

# Current production Railway public domain. It is used only as a fallback when
# Railway does not expose RAILWAY_PUBLIC_DOMAIN inside the running container.
# Explicit MINIAPP_PUBLIC_URL / Railway env values still have priority.
_PRODUCTION_FALLBACK = "https://w-production.up.railway.app/miniapp/"


def install_miniapp_url_fallback() -> None:
    from app import miniapp_server

    if getattr(miniapp_server, "_production_url_fallback_installed", False):
        return

    original = miniapp_server.miniapp_public_url

    def resolved_url() -> str:
        return original() or _PRODUCTION_FALLBACK

    # miniapp_server resolves this global at request time.
    miniapp_server.miniapp_public_url = resolved_url

    # miniapp_bot_ui imported the function by name, so patch its local reference
    # too. This makes /app and the WebApp keyboard button work after redeploy.
    try:
        from app import miniapp_bot_ui
        miniapp_bot_ui.miniapp_public_url = resolved_url
    except Exception:
        log.exception("Could not patch Mini App bot UI URL reference")

    miniapp_server._production_url_fallback_installed = True
    log.info("Mini App public URL resolved to %s", resolved_url())
