import base64
import html
import logging
import os
import re
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from telethon.tl import types
from telethon.utils import get_peer_id

from app.config import settings
from app.formatting import post_html

log = logging.getLogger("telegram-ai-news.user-publisher")

_client = None
_status = {
    "configured": False,
    "online": False,
    "premium": False,
    "username": "",
    "error": "",
}


def _session_b64() -> str | None:
    direct = (getattr(settings, "telegram_publisher_session_file_b64", None) or "").strip()
    if direct:
        return direct
    chunks = []
    index = 1
    while True:
        value = os.getenv(f"TELEGRAM_PUBLISHER_SESSION_FILE_B64_{index}")
        if not value:
            break
        chunks.append(value.strip())
        index += 1
    return "".join(chunks) if chunks else None


def publisher_configured() -> bool:
    return bool(_session_b64())


def get_user_publisher_status() -> dict:
    return dict(_status)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)}"[:700]


def _build_client():
    global _client
    if _client is not None:
        return _client
    payload = _session_b64()
    if not payload:
        return None

    from opentele2.tl import TelegramClient as DesktopTelegramClient

    session_path = Path(getattr(settings, "publisher_session_file_path", "data/telegram_publisher.session"))
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_bytes(base64.b64decode(payload))
    # app.config installs the same dedicated SOCKS5 on all Telethon clients.
    _client = DesktopTelegramClient(str(session_path))
    return _client


async def initialize_user_publisher() -> dict:
    payload = _session_b64()
    if not payload:
        _status.update(configured=False, online=False, premium=False, username="", error="")
        return get_user_publisher_status()

    _status["configured"] = True
    try:
        client = _build_client()
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Publisher .session is not authorized")
        me = await client.get_me()
        premium = bool(getattr(me, "premium", False))
        username = getattr(me, "username", None) or str(getattr(me, "id", ""))
        if not premium:
            raise RuntimeError("Publisher Telegram account does not have Premium")
        _status.update(
            configured=True,
            online=True,
            premium=True,
            username=str(username),
            error="",
        )
        log.info("Premium user publisher ONLINE account=@%s", username)
    except Exception as exc:
        _status.update(configured=True, online=False, premium=False, error=_safe_error(exc))
        log.exception("Premium user publisher startup check failed")
    return get_user_publisher_status()


async def _ready_client():
    state = await initialize_user_publisher()
    if not state.get("configured"):
        return None
    if not state.get("online"):
        raise RuntimeError(f"PREMIUM_USER_PUBLISHER_OFFLINE: {state.get('error') or 'unknown error'}")
    return _client


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


class _TelegramHTMLParser(HTMLParser):
    """Convert our safe Telegram HTML into MTProto text + formatting entities."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.units = 0
        self.stack: list[dict] = []
        self.entities: list = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.units += _utf16_len(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "br":
            self.handle_data("\n")
            return
        aliases = {"strong": "b", "em": "i", "strike": "s", "del": "s", "tg-spoiler": "spoiler"}
        tag = aliases.get(tag, tag)
        if tag not in {"b", "i", "u", "s", "spoiler", "blockquote", "a", "tg-emoji"}:
            return
        entry = {"tag": tag, "start": self.units}
        if tag == "a":
            entry["href"] = attrs_dict.get("href", "")
        elif tag == "tg-emoji":
            value = attrs_dict.get("emoji-id", "")
            if value.isdigit():
                entry["emoji_id"] = int(value)
            else:
                return
        self.stack.append(entry)

    def handle_endtag(self, tag: str) -> None:
        aliases = {"strong": "b", "em": "i", "strike": "s", "del": "s", "tg-spoiler": "spoiler"}
        tag = aliases.get(tag.lower(), tag.lower())
        match_index = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                match_index = index
                break
        if match_index is None:
            return
        entry = self.stack.pop(match_index)
        length = self.units - int(entry["start"])
        if length <= 0:
            return
        offset = int(entry["start"])
        if tag == "b":
            entity = types.MessageEntityBold(offset, length)
        elif tag == "i":
            entity = types.MessageEntityItalic(offset, length)
        elif tag == "u":
            entity = types.MessageEntityUnderline(offset, length)
        elif tag == "s":
            entity = types.MessageEntityStrike(offset, length)
        elif tag == "spoiler":
            entity = types.MessageEntitySpoiler(offset, length)
        elif tag == "blockquote":
            entity = types.MessageEntityBlockquote(offset, length)
        elif tag == "a":
            href = entry.get("href") or ""
            if not href:
                return
            entity = types.MessageEntityTextUrl(offset, length, href)
        elif tag == "tg-emoji":
            entity = types.MessageEntityCustomEmoji(offset, length, int(entry["emoji_id"]))
        else:
            return
        self.entities.append(entity)


def telegram_html_to_mtproto(value: str) -> tuple[str, list]:
    parser = _TelegramHTMLParser()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts), sorted(parser.entities, key=lambda e: (e.offset, -e.length))


async def _resolve_target(client, channel_ref):
    ref = str(channel_ref or "").strip()
    if not ref:
        raise RuntimeError("Publication channel is empty")
    if ref.startswith("@") or re.fullmatch(r"[A-Za-z0-9_]{4,64}", ref):
        return await client.get_entity(ref)

    if re.fullmatch(r"-?\d+", ref):
        wanted = int(ref)
        async for dialog in client.iter_dialogs():
            try:
                ids = {int(dialog.id), int(get_peer_id(dialog.entity))}
            except Exception:
                ids = {int(dialog.id)}
            if wanted in ids:
                return dialog.entity
        raise RuntimeError(
            f"Premium publisher account cannot resolve channel {ref}. Add this account as an admin of the channel."
        )
    return await client.get_entity(ref)


async def _download_bot_media(bot, file_id: str, media_type: str, index: int) -> BytesIO:
    tg_file = await bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    if not data:
        raise RuntimeError("Bot API returned empty media while preparing Premium-user publication")
    suffix = ".mp4" if media_type == "video" else ".jpg"
    buf = BytesIO(data)
    buf.name = f"sports_news_{index}{suffix}"
    return buf


async def _send_with_user(client, bot, destination, row: dict) -> None:
    from app import album_support

    entity = await _resolve_target(client, destination)
    message, entities = telegram_html_to_mtproto(post_html(row.get("rewritten_text") or ""))
    items = await album_support.get_news_media(int(row["id"]))
    buffers: list[BytesIO] = []
    try:
        if items:
            for index, item in enumerate(items, 1):
                file_id = item.get("edited_file_id") or item["original_file_id"]
                buffers.append(await _download_bot_media(bot, file_id, item["media_type"], index))
            if len(buffers) == 1:
                await client.send_file(
                    entity,
                    buffers[0],
                    caption=message,
                    formatting_entities=entities,
                    supports_streaming=items[0]["media_type"] == "video",
                )
            else:
                captions = [message] + [""] * (len(buffers) - 1)
                formatting = [entities] + [[] for _ in range(len(buffers) - 1)]
                await client.send_file(
                    entity,
                    buffers,
                    caption=captions,
                    formatting_entities=formatting,
                    supports_streaming=True,
                )
            return

        media_type = row.get("media_type")
        file_id = row.get("media_file_id")
        if media_type in {"photo", "video"} and file_id:
            buf = await _download_bot_media(bot, file_id, media_type, 1)
            buffers.append(buf)
            await client.send_file(
                entity,
                buf,
                caption=message,
                formatting_entities=entities,
                supports_streaming=media_type == "video",
            )
            return

        await client.send_message(entity, message, formatting_entities=entities, link_preview=False)
    finally:
        for buf in buffers:
            try:
                buf.close()
            except Exception:
                pass


async def publish_row_via_user(bot, row: dict) -> None:
    """Use a Premium Telegram user account when configured; otherwise preserve Bot API publishing."""
    client = await _ready_client()
    if client is None:
        return await _fallback_publish_row(bot, row)

    from app.channel_workspace import get_target_for_source

    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    target = await get_target_for_source(user_id, row.get("source"))
    if not target:
        raise RuntimeError("TARGET_CHANNEL_NOT_CONFIGURED: add a publishing channel in 'Мої канали'")
    await _send_with_user(client, bot, target["channel_ref"], row)


def _status_html() -> str:
    state = get_user_publisher_status()
    if not state.get("configured"):
        return "⚪ NOT CONFIGURED"
    if state.get("online"):
        username = html.escape(str(state.get("username") or "account"))
        return f"🟢 ONLINE · <code>@{username}</code> · PREMIUM"
    return f"🔴 OFFLINE · <code>{html.escape(str(state.get('error') or 'unknown error')[:180])}</code>"


def install_user_publisher_status_runtime(app_main) -> None:
    if getattr(app_main, "_user_publisher_status_installed", False):
        return
    original_notify = app_main.notify_user

    async def notify_with_publisher(user_id: int, text: str, reply_markup=None):
        if "SPORTS NEWS CONTROL" in (text or "") and "Premium publisher:" not in text:
            lines = text.split("\n")
            insert_at = 3 if len(lines) >= 3 else len(lines)
            lines.insert(insert_at, f"Premium publisher: <b>{_status_html()}</b>")
            text = "\n".join(lines)
        return await original_notify(user_id, text, reply_markup)

    app_main.notify_user = notify_with_publisher
    app_main._user_publisher_status_installed = True


def install_user_publisher() -> None:
    """Patch every final publication path after album + per-channel routing are installed."""
    global _fallback_publish_row
    from app import album_support, channel_workspace, publish_ui, publishing

    if getattr(publishing, "_premium_user_publisher_installed", False):
        return
    _fallback_publish_row = publishing.publish_row

    publishing.publish_row = publish_row_via_user
    publish_ui.publish_row = publish_row_via_user
    album_support.publish_row = publish_row_via_user
    channel_workspace.routed_publish_row = publish_row_via_user
    publishing._premium_user_publisher_installed = True
    log.info("Installed Premium Telegram user-account publisher (configured=%s)", publisher_configured())
