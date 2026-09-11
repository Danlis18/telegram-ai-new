import asyncio
import logging
import re
from urllib.parse import quote

log = logging.getLogger("telegram-ai-news.telegram-proxy")

_RUNTIME_STATUS: dict = {
    "configured": False,
    "ok": True,
    "status": "DISABLED",
    "endpoint": "",
    "error": "",
}


class ProxyUnavailableError(RuntimeError):
    pass


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


def _safe_error(exc: BaseException, settings) -> str:
    text = f"{type(exc).__name__}: {str(exc)}"[:900]
    for secret in (
        getattr(settings, "telegram_proxy_user", None),
        getattr(settings, "telegram_proxy_password", None),
    ):
        if secret:
            text = text.replace(str(secret), "***")
    return text


def get_proxy_runtime_status() -> dict:
    return dict(_RUNTIME_STATUS)


def _set_runtime_status(*, configured: bool, ok: bool, status: str, endpoint: str = "", error: str = "") -> dict:
    _RUNTIME_STATUS.update(
        {
            "configured": bool(configured),
            "ok": bool(ok),
            "status": str(status),
            "endpoint": str(endpoint),
            "error": str(error),
        }
    )
    return get_proxy_runtime_status()


async def check_telegram_proxy(settings, timeout: float = 8.0) -> dict:
    """Verify the configured SOCKS5 can reach Telegram before the reader starts.

    This does not use the Telegram session/auth key. It only opens a TCP tunnel
    through the SOCKS5 proxy to Telegram DC endpoints, so it is safe to run on
    every Railway boot before the reader is allowed to connect.
    """
    try:
        proxy_cfg = _proxy_config(settings)
    except Exception as exc:
        error = _safe_error(exc, settings)
        log.error("Telegram proxy configuration invalid: %s", error)
        return _set_runtime_status(
            configured=True,
            ok=False,
            status="CONFIG_ERROR",
            error=error,
        )

    if not proxy_cfg:
        return _set_runtime_status(configured=False, ok=True, status="DISABLED")

    endpoint = f"{proxy_cfg['addr']}:{proxy_cfg['port']}"
    username = proxy_cfg.get("username")
    password = proxy_cfg.get("password") or ""
    auth = ""
    if username:
        auth = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@"
    proxy_url = f"socks5://{auth}{proxy_cfg['addr']}:{proxy_cfg['port']}"

    try:
        from python_socks.async_.asyncio import Proxy
    except Exception as exc:
        error = _safe_error(exc, settings)
        log.exception("python-socks is unavailable")
        return _set_runtime_status(
            configured=True,
            ok=False,
            status="DEPENDENCY_ERROR",
            endpoint=endpoint,
            error=error,
        )

    last_error = None
    # Two Telegram DC endpoints avoid declaring the proxy dead because of a
    # temporary route issue to one single DC.
    for dest_host, dest_port in (("149.154.167.51", 443), ("149.154.167.50", 443)):
        sock = None
        try:
            proxy = Proxy.from_url(proxy_url)
            sock = await asyncio.wait_for(
                proxy.connect(dest_host=dest_host, dest_port=dest_port),
                timeout=timeout,
            )
            log.info(
                "Telegram SOCKS5 preflight OK proxy=%s target=%s:%s",
                endpoint,
                dest_host,
                dest_port,
            )
            return _set_runtime_status(
                configured=True,
                ok=True,
                status="ONLINE",
                endpoint=endpoint,
                error="",
            )
        except Exception as exc:
            last_error = exc
            log.warning(
                "Telegram SOCKS5 preflight failed proxy=%s target=%s:%s error=%s",
                endpoint,
                dest_host,
                dest_port,
                _safe_error(exc, settings),
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    error = _safe_error(last_error or RuntimeError("unknown proxy error"), settings)
    return _set_runtime_status(
        configured=True,
        ok=False,
        status="OFFLINE",
        endpoint=endpoint,
        error=error,
    )


def assert_proxy_ready() -> None:
    state = get_proxy_runtime_status()
    if state.get("configured") and not state.get("ok"):
        raise ProxyUnavailableError(state.get("error") or "SOCKS5 proxy preflight failed")


def _status_html() -> str:
    state = get_proxy_runtime_status()
    status = state.get("status") or "UNKNOWN"
    if status == "ONLINE":
        label = "🟢 ONLINE"
    elif status == "DISABLED":
        label = "⚪ DISABLED"
    elif status in {"CONFIG_ERROR", "DEPENDENCY_ERROR"}:
        label = f"🔴 {status}"
    else:
        label = "🔴 OFFLINE"
    endpoint = state.get("endpoint") or ""
    return f"{label}" + (f" · <code>{endpoint}</code>" if endpoint else "")


def install_proxy_status_runtime(app_main) -> None:
    """Expose proxy status in owner notifications and the control panel."""
    if getattr(app_main, "_proxy_status_runtime_installed", False):
        return

    original_notify = app_main.notify_user

    async def notify_user_with_proxy(user_id: int, text: str, reply_markup=None):
        if "SPORTS NEWS CONTROL" in (text or "") and "SOCKS5 Proxy:" not in text:
            lines = text.split("\n")
            insert_at = 2 if len(lines) >= 2 else len(lines)
            lines.insert(insert_at, f"SOCKS5 Proxy: <b>{_status_html()}</b>")
            text = "\n".join(lines)
        return await original_notify(user_id, text, reply_markup)

    app_main.notify_user = notify_user_with_proxy

    try:
        from app import publish_ui

        original_show_control = publish_ui.show_control_panel

        class QueryWithProxyStatus:
            def __init__(self, query):
                self._query = query

            def __getattr__(self, name):
                return getattr(self._query, name)

            async def edit_message_text(self, text, *args, **kwargs):
                if "⚙️ <b>Керування SPORTS NEWS</b>" in (text or "") and "SOCKS5 Proxy:" not in text:
                    marker = "\n\n"
                    replacement = f"\n\nSOCKS5 Proxy: <b>{_status_html()}</b>\n"
                    text = text.replace(marker, replacement, 1)
                return await self._query.edit_message_text(text, *args, **kwargs)

        async def show_control_with_proxy(query):
            return await original_show_control(QueryWithProxyStatus(query))

        publish_ui.show_control_panel = show_control_with_proxy
    except Exception:
        log.exception("Could not install proxy status into control panel")

    app_main._proxy_status_runtime_installed = True


def install_telegram_reader_proxy(settings) -> None:
    """Inject a SOCKS5 proxy into Telethon clients used by the reader.

    The admin Bot API client and OpenAI/httpx clients are not affected. opentele2's
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
