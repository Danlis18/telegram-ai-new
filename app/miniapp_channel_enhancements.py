import hashlib
import hmac
import logging
import re
import time
from typing import Any

import aiosqlite
from fastapi import Depends, HTTPException
from fastapi.responses import Response

from app.auth import is_authorized_id
from app.config import settings
from app.miniapp_server import app, current_user
from app.persistence_runtime import storage_status

log = logging.getLogger("telegram-ai-news.miniapp-channels")
_installed = False
_avatar_cache: dict[str, tuple[float, bytes]] = {}


def _avatar_sig(uid: int, kind: str, item_id: int, exp: int) -> str:
    payload = f"{int(uid)}:{kind}:{int(item_id)}:{int(exp)}"
    return hmac.new(settings.telegram_bot_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _avatar_url(uid: int, kind: str, item_id: int) -> str:
    exp = int(time.time()) + 86400
    sig = _avatar_sig(uid, kind, item_id, exp)
    return f"/api/channel-avatar/{kind}/{int(item_id)}?uid={int(uid)}&exp={exp}&sig={sig}"


async def _lookup_ref(uid: int, kind: str, item_id: int) -> str | None:
    table = "user_sources" if kind == "source" else "user_targets"
    column = "username" if kind == "source" else "channel_ref"
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            f"SELECT {column} FROM {table} WHERE id=? AND user_id=? AND is_active=1 LIMIT 1",
            (int(item_id), int(uid)),
        )
        row = await cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _public_username(value: str | None) -> str | None:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I)
    value = value.split("?", 1)[0].strip("/ ").lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,64}", value):
        return value
    return None


async def _download_avatar(username: str) -> bytes | None:
    cache_key = username.lower()
    cached = _avatar_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]

    data: bytes | None = None
    try:
        from app import main as app_main
        entity = await app_main.reader.get_entity("@" + username)
        downloaded = await app_main.reader.download_profile_photo(entity, file=bytes)
        if isinstance(downloaded, (bytes, bytearray)) and downloaded:
            data = bytes(downloaded)
    except Exception:
        log.debug("Reader could not fetch avatar for @%s", username, exc_info=True)

    if not data:
        try:
            from app import main as app_main
            chat = await app_main.publisher.get_chat("@" + username)
            photo = getattr(chat, "photo", None)
            file_id = getattr(photo, "big_file_id", None) or getattr(photo, "small_file_id", None)
            if file_id:
                tg_file = await app_main.publisher.get_file(file_id)
                data = bytes(await tg_file.download_as_bytearray())
        except Exception:
            log.debug("Bot API could not fetch avatar for @%s", username, exc_info=True)

    if data:
        _avatar_cache[cache_key] = (time.time() + 21600, data)
    return data


def install_channel_miniapp_enhancements() -> None:
    global _installed
    if _installed:
        return

    @app.get("/api/channel-avatars")
    async def channel_avatars(user=Depends(current_user)):
        uid = int(user["id"])
        async with aiosqlite.connect(settings.database_path) as db:
            cur = await db.execute(
                "SELECT id FROM user_sources WHERE user_id=? AND is_active=1 ORDER BY id",
                (uid,),
            )
            sources = {str(int(row[0])): _avatar_url(uid, "source", int(row[0])) for row in await cur.fetchall()}
            cur = await db.execute(
                "SELECT id FROM user_targets WHERE user_id=? AND is_active=1 ORDER BY id",
                (uid,),
            )
            targets = {str(int(row[0])): _avatar_url(uid, "target", int(row[0])) for row in await cur.fetchall()}
        return {"sources": sources, "targets": targets}

    @app.get("/api/channel-avatar/{kind}/{item_id}")
    async def channel_avatar(kind: str, item_id: int, uid: int, exp: int, sig: str):
        if kind not in {"source", "target"}:
            raise HTTPException(status_code=404, detail="Unknown channel type")
        if not is_authorized_id(uid):
            raise HTTPException(status_code=403, detail="Access denied")
        if exp < int(time.time()) or not hmac.compare_digest(sig, _avatar_sig(uid, kind, item_id, exp)):
            raise HTTPException(status_code=403, detail="Avatar link expired")
        ref = await _lookup_ref(uid, kind, item_id)
        username = _public_username(ref)
        if not username:
            raise HTTPException(status_code=404, detail="Public username unavailable")
        data = await _download_avatar(username)
        if not data:
            raise HTTPException(status_code=404, detail="Avatar unavailable")
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=21600"},
        )

    @app.get("/healthz/storage")
    async def storage_health() -> dict[str, Any]:
        return storage_status()

    _installed = True
    log.info("Mini App channel enhancements installed")
