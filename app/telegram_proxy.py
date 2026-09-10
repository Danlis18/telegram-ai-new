import logging

log = logging.getLogger("telegram-ai-news.telegram-proxy")


def _proxy_config(settings) -> dict | None:
    host = (getattr(settings, "telegram_proxy_host", None) or "").strip()
    port = getattr(settings, "telegram_proxy_port", None)
    if not host and not port:
        return None
    if not host or not port:
        raise RuntimeError("TELEGRAM_PROXY_HOST and TELEGRAM_PROXY_PORT must be set together")

    proxy = {
        "proxy_type": "socks5",
        "addr": host,
        "port": int(port),
        "rdns": bool(getattr(settings, "telegram_proxy_rdns", True)),
    }
    username = (getattr(settings, "telegram_proxy_user", None) or "").strip()
    password = getattr(settings, "telegram_proxy_password", None)
    if username:
        proxy["username"] = username
        proxy["password"] = password or ""
    return proxy


def install_telegram_reader_proxy(settings) -> None:
    """Inject a SOCKS5 proxy into Telethon clients used by the reader.

    The admin Bot API client and OpenAI/httpx clients are not affected.  opentele2's
    TelegramClient extends Telethon's client, so the same constructor patch also
    covers the uploaded .session-file path used by this project.
    """
    proxy = _proxy_config(settings)
    if not proxy:
        return

    from telethon import TelegramClient

    if getattr(TelegramClient, "_sports_news_socks5_patch", False):
        return

    original_init = TelegramClient.__init__

    def proxy_init(self, *args, **kwargs):
        if kwargs.get("proxy") is None:
            kwargs["proxy"] = dict(proxy)
        return original_init(self, *args, **kwargs)

    TelegramClient.__init__ = proxy_init
    TelegramClient._sports_news_socks5_patch = True

    log.info(
        "Telegram reader SOCKS5 proxy enabled: %s:%s auth=%s rdns=%s",
        proxy["addr"],
        proxy["port"],
        "yes" if proxy.get("username") else "no",
        proxy["rdns"],
    )
