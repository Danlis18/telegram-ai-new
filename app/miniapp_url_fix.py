import logging

log = logging.getLogger("telegram-ai-news.miniapp-url")


def install_miniapp_url_fallback() -> None:
    """Resolve Mini App URL only from the live Railway environment.

    Never fall back to a previously hardcoded Railway domain: Railway domains can
    be reprovisioned/replaced, and a stale fallback makes Telegram's launcher open
    a dead deployment. miniapp_server.miniapp_public_url() already resolves
    MINIAPP_PUBLIC_URL, RAILWAY_PUBLIC_DOMAIN and RAILWAY_STATIC_URL dynamically.
    """
    from app import miniapp_server

    if getattr(miniapp_server, "_production_url_fallback_installed", False):
        return

    original = miniapp_server.miniapp_public_url

    def resolved_url() -> str | None:
        return original()

    # miniapp_server resolves this global at request time.
    miniapp_server.miniapp_public_url = resolved_url

    # miniapp_bot_ui imported the function by name, so patch its local reference
    # too. This keeps /app and the in-chat WebApp button synchronized with the
    # currently provisioned Railway domain.
    try:
        from app import miniapp_bot_ui
        miniapp_bot_ui.miniapp_public_url = resolved_url
    except Exception:
        log.exception("Could not patch Mini App bot UI URL reference")

    miniapp_server._production_url_fallback_installed = True
    current = resolved_url()
    if current:
        log.info("Mini App public URL resolved dynamically to %s", current)
    else:
        log.warning("Mini App public URL is not configured in Railway environment")
