import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import aiosqlite
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import is_authorized_id, is_owner_id
from app.config import settings
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.miniapp")
STATIC_DIR = Path(__file__).resolve().parent.parent / "miniapp"
_server_task: asyncio.Task | None = None

_CUSTOM_EMOJI_RE = re.compile(
    r'<tg-emoji\s+emoji-id=["\'](?P<id>\d+)["\']\s*>(?P<fallback>.*?)</tg-emoji>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def miniapp_public_url() -> str | None:
    explicit = (os.getenv("MINIAPP_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        if not explicit.startswith(("https://", "http://")):
            explicit = "https://" + explicit
        return explicit + "/miniapp/"
    domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_URL") or "").strip().rstrip("/")
    if not domain:
        return None
    if not domain.startswith(("https://", "http://")):
        domain = "https://" + domain
    return domain + "/miniapp/"


def _validate_init_data(raw: str) -> dict[str, Any]:
    if not raw:
        raise HTTPException(status_code=401, detail="Telegram initData відсутній")
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Некоректний Telegram initData")
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram signature відсутній")
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", settings.telegram_bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="Telegram signature не пройшла перевірку")
    try:
        auth_date = int(pairs.get("auth_date") or 0)
    except Exception:
        auth_date = 0
    if not auth_date or abs(int(time.time()) - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Telegram сесія Mini App застаріла")
    try:
        user = json.loads(pairs.get("user") or "{}")
        user_id = int(user.get("id") or 0)
    except Exception:
        user, user_id = {}, 0
    if not user_id or not is_authorized_id(user_id):
        raise HTTPException(status_code=403, detail="Немає доступу до Auto Posting")
    return {"id": user_id, "user": user, "raw": pairs}


async def current_user(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")) -> dict[str, Any]:
    return _validate_init_data(x_telegram_init_data)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", value or "")).strip()


def _premium_count(value: str) -> int:
    return len(_CUSTOM_EMOJI_RE.findall(value or ""))


def _sign_media(uid: int, news_id: int, position: int, original: bool, exp: int) -> str:
    payload = f"{int(uid)}:{int(news_id)}:{int(position)}:{1 if original else 0}:{int(exp)}"
    return hmac.new(settings.telegram_bot_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _media_url(uid: int, news_id: int, position: int, original: bool = False) -> str:
    exp = int(time.time()) + 900
    sig = _sign_media(uid, news_id, position, original, exp)
    return f"/api/media/{int(news_id)}/{int(position)}?uid={int(uid)}&original={1 if original else 0}&exp={exp}&sig={sig}"


async def _post_media(news_id: int, uid: int) -> list[dict]:
    from app import album_support
    items = await album_support.get_news_media(int(news_id))
    if items:
        return [
            {
                "position": int(item.get("position") or index),
                "type": item.get("media_type") or "photo",
                "edited": bool(item.get("edited_file_id")),
                "url": _media_url(uid, news_id, int(item.get("position") or index), False),
                "original_url": _media_url(uid, news_id, int(item.get("position") or index), True),
            }
            for index, item in enumerate(items)
        ]
    from app.database import get_news
    with user_scope(uid):
        row = await get_news(int(news_id))
    if not row or not row.get("media_file_id"):
        return []
    return [{
        "position": 0,
        "type": row.get("media_type") or "photo",
        "edited": bool(row.get("original_media_file_id") and row.get("media_file_id") != row.get("original_media_file_id")),
        "url": _media_url(uid, news_id, 0, False),
        "original_url": _media_url(uid, news_id, 0, True),
    }]


async def _serialize_post(row: dict, uid: int, *, full: bool = False) -> dict:
    text = row.get("rewritten_text") or ""
    from app.channel_workspace import get_target_for_source
    target = await get_target_for_source(uid, row.get("source"))
    data = {
        "id": int(row["id"]),
        "source": row.get("source") or "",
        "score": int(row.get("score") or 0),
        "status": row.get("status") or "",
        "created_at": row.get("created_at"),
        "scheduled_at": row.get("scheduled_at"),
        "published_at": row.get("published_at"),
        "text_plain": _plain(text),
        "text_html": text,
        "premium_emoji_count": _premium_count(text),
        "target": ({"id": target.get("id"), "title": target.get("title") or target.get("channel_ref"), "channel_ref": target.get("channel_ref")} if target else None),
        "media": await _post_media(int(row["id"]), uid),
    }
    if full:
        data["original_text"] = row.get("original_text") or ""
    return data


async def _premium_palette(uid: int, limit: int = 24) -> list[dict]:
    found: dict[str, str] = {}
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT rewritten_text FROM news WHERE user_id=? AND rewritten_text IS NOT NULL ORDER BY id DESC LIMIT 80",
            (int(uid),),
        )
        values = [row[0] or "" for row in await cur.fetchall()]
        cur = await db.execute(
            "SELECT corrected_text FROM editorial_feedback WHERE user_id=? ORDER BY id DESC LIMIT 40",
            (int(uid),),
        )
        values.extend(row[0] or "" for row in await cur.fetchall())
    for value in values:
        for match in _CUSTOM_EMOJI_RE.finditer(value):
            emoji_id = match.group("id")
            fallback = _plain(match.group("fallback")) or "✨"
            if emoji_id not in found:
                found[emoji_id] = fallback[:8]
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    return [{"id": key, "fallback": value} for key, value in found.items()]


app = FastAPI(title="Auto Posting Mini App", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
        "font-src 'self' data:; frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'self'"
    )
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "auto-posting-miniapp"}


@app.get("/")
async def root():
    return RedirectResponse("/miniapp/")


@app.get("/api/bootstrap")
async def bootstrap(user=Depends(current_user)):
    uid = int(user["id"])
    from app import main as app_main
    from app.content_policy import quota_state
    from app.database import count_user_sources, get_setting, list_user_targets, stats
    from app.publishing import get_photo_edit_mode, get_publish_mode
    from app.user_publisher import get_user_publisher_status
    with user_scope(uid):
        s = await stats()
        quotas = await quota_state(uid)
        targets = await list_user_targets(uid)
        source_count = await count_user_sources(uid)
        publish_mode = await get_publish_mode()
        photo_mode = await get_photo_edit_mode()
        paused = (await get_setting("processing_paused", "false") or "false").lower() == "true"
        palette = await _premium_palette(uid)
    publisher = get_user_publisher_status()
    try:
        reader_online = bool(app_main.reader.is_connected())
    except Exception:
        reader_online = False
    return {
        "user": user["user"],
        "owner": is_owner_id(uid),
        "stats": s,
        "quotas": quotas,
        "workspace": {"targets": len(targets), "sources": source_count},
        "modes": {"publish": publish_mode, "photo": photo_mode, "paused": paused},
        "system": {
            "reader_online": reader_online,
            "premium_publisher_online": bool(publisher.get("online")),
            "premium": bool(publisher.get("premium")),
            "publisher_username": publisher.get("username") or "",
        },
        "premium_palette": palette,
        "app_url": miniapp_public_url(),
    }


@app.get("/api/posts")
async def posts(tab: str = Query("ready"), limit: int = Query(30, ge=1, le=80), user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import get_archive, get_queue, get_scheduled
    with user_scope(uid):
        if tab == "scheduled": rows = await get_scheduled(limit)
        elif tab == "archive": rows = await get_archive(limit)
        else: rows = await get_queue(limit)
        items = [await _serialize_post(row, uid) for row in rows]
    return {"items": items, "tab": tab}


@app.get("/api/posts/{news_id}")
async def post_detail(news_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import get_news
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        return await _serialize_post(row, uid, full=True)


class EditTextBody(BaseModel):
    html: str = Field(min_length=1, max_length=12000)


@app.post("/api/posts/{news_id}/edit-text")
async def edit_text(news_id: int, body: EditTextBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app import admin_bot
    from app.database import get_news, save_editorial_feedback, update_news
    from app.formatting import _safe_telegram_html
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        corrected = admin_bot._clean_editor_text(_safe_telegram_html(body.html)).strip()
        if len(_plain(corrected)) < 12:
            raise HTTPException(status_code=400, detail="Текст занадто короткий")
        await save_editorial_feedback(news_id, row.get("original_text") or "", row.get("rewritten_text") or "", corrected)
        await update_news(news_id, rewritten_text=corrected, status="ready")
        updated = await get_news(news_id)
        return await _serialize_post(updated or row, uid, full=True)


@app.post("/api/posts/{news_id}/regenerate")
async def regenerate(news_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app import ai_editor
    from app.database import get_news, update_news
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        result = await ai_editor.rewrite_news(row.get("original_text") or "", row.get("source") or "")
        if not result.get("publish") or not (result.get("text") or "").strip():
            raise HTTPException(status_code=409, detail=result.get("reason") or "AI не рекомендує цей пост")
        await update_news(news_id, rewritten_text=result["text"].strip(), score=int(result.get("score") or row.get("score") or 0), status="ready")
        updated = await get_news(news_id)
        return await _serialize_post(updated or row, uid, full=True)


@app.post("/api/posts/{news_id}/publish")
async def publish_post(news_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app import main as app_main, publishing
    from app.database import get_news, update_news
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        if row.get("status") == "published": return {"ok": True, "already": True}
        await publishing.publish_row(app_main.publisher, row)
        await update_news(news_id, status="published", published_at=publishing.utc_now_db(), scheduled_at=None)
    return {"ok": True}


class RejectBody(BaseModel):
    reason: str = Field(default="Не підходить", max_length=1200)


@app.post("/api/posts/{news_id}/reject")
async def reject_post(news_id: int, body: RejectBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.content_policy import create_replacement_request, current_period, save_rejection_feedback
    from app.database import get_news, update_news
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        reason = body.reason.strip() or "Не підходить"
        await update_news(news_id, status="skipped")
        await save_rejection_feedback(row, reason, uid)
        await create_replacement_request(uid, news_id, current_period())
    return {"ok": True}


class ScheduleBody(BaseModel):
    local_datetime: str


@app.post("/api/posts/{news_id}/schedule")
async def schedule_post(news_id: int, body: ScheduleBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import get_news, update_news
    try:
        local = datetime.fromisoformat(body.local_datetime)
        tz = ZoneInfo(settings.publish_timezone or "Europe/Kyiv")
        if local.tzinfo is None: local = local.replace(tzinfo=tz)
        scheduled = local.astimezone(timezone.utc)
        if scheduled <= datetime.now(timezone.utc):
            raise ValueError("past")
    except Exception:
        raise HTTPException(status_code=400, detail="Обери майбутню дату і час")
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Пост не знайдено")
        await update_news(news_id, status="scheduled", scheduled_at=scheduled.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True}


@app.get("/api/channels")
async def channels(user=Depends(current_user)):
    uid = int(user["id"])
    from app.channel_workspace import _ensure_user_routes, _route_map
    from app.database import list_user_sources, list_user_targets
    with user_scope(uid):
        await _ensure_user_routes(uid)
        targets = await list_user_targets(uid)
        sources = await list_user_sources(uid)
        routes = await _route_map(uid)
    target_items = []
    for t in targets:
        tid = int(t["id"])
        ref = str(t.get("channel_ref") or "")
        url = f"https://t.me/{ref.lstrip('@')}" if (ref.startswith("@") or re.fullmatch(r"[A-Za-z0-9_]{4,64}", ref)) else None
        target_items.append({**t, "id": tid, "url": url, "source_count": sum(1 for route in routes.values() if route and int(route["id"]) == tid)})
    source_items = []
    for s in sources:
        username = str(s["username"]).lower()
        target = routes.get(username)
        source_items.append({**s, "id": int(s["id"]), "target_id": int(target["id"]) if target else None, "url": f"https://t.me/{username}"})
    return {"targets": target_items, "sources": source_items}


class TargetBody(BaseModel):
    channel_ref: str = Field(min_length=2, max_length=180)
    title: str = Field(default="", max_length=160)


def _normalize_target_ref(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/", "@", value, flags=re.I)
    value = value.split("?", 1)[0].strip("/ ")
    return value


@app.post("/api/channels/targets")
async def add_target(body: TargetBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import add_user_target
    ref = _normalize_target_ref(body.channel_ref)
    if not ref: raise HTTPException(status_code=400, detail="Вкажи канал")
    with user_scope(uid):
        target_id = await add_user_target(uid, ref, body.title)
    return {"ok": True, "id": target_id}


class SourceBody(BaseModel):
    username: str
    target_id: int


@app.post("/api/channels/sources")
async def add_source(body: SourceBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.channel_workspace import assign_source_to_target
    try:
        with user_scope(uid):
            await assign_source_to_target(uid, body.username, body.target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/channels/targets/{target_id}/default")
async def default_target(target_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import set_default_target
    with user_scope(uid):
        await set_default_target(target_id, uid)
    return {"ok": True}


class RouteBody(BaseModel):
    target_id: int


@app.post("/api/channels/sources/{source_id}/route")
async def route_source(source_id: int, body: RouteBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.channel_workspace import assign_source_to_target
    from app.database import list_user_sources
    with user_scope(uid):
        sources = await list_user_sources(uid, active_only=False)
        source = next((s for s in sources if int(s["id"]) == int(source_id)), None)
        if not source: raise HTTPException(status_code=404, detail="Джерело не знайдено")
        await assign_source_to_target(uid, source["username"], body.target_id)
    return {"ok": True}


@app.delete("/api/channels/sources/{source_id}")
async def delete_source(source_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app.channel_workspace import _delete_source
    with user_scope(uid): await _delete_source(uid, source_id)
    return {"ok": True}


@app.delete("/api/channels/targets/{target_id}")
async def delete_target(target_id: int, user=Depends(current_user)):
    uid = int(user["id"])
    from app.channel_workspace import _delete_target
    with user_scope(uid): await _delete_target(uid, target_id)
    return {"ok": True}


class QuotaBody(BaseModel):
    quota: int = Field(ge=0, le=20)
    delay_minutes: int = Field(ge=0, le=360)


@app.put("/api/quotas/{period_key}")
async def update_quota(period_key: str, body: QuotaBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.content_policy import PERIODS, set_period_delay, set_period_quota
    if period_key not in PERIODS: raise HTTPException(status_code=404, detail="Період не знайдено")
    with user_scope(uid):
        quota = await set_period_quota(period_key, body.quota)
        delay = await set_period_delay(period_key, body.delay_minutes)
    return {"ok": True, "quota": quota, "delay_minutes": delay}


class ModesBody(BaseModel):
    publish_mode: str | None = None
    photo_edit_mode: str | None = None
    processing_paused: bool | None = None


@app.patch("/api/modes")
async def modes(body: ModesBody, user=Depends(current_user)):
    uid = int(user["id"])
    from app.database import set_setting
    with user_scope(uid):
        if body.publish_mode is not None:
            if body.publish_mode not in {"auto", "manual"}: raise HTTPException(status_code=400, detail="Некоректний режим публікації")
            await set_setting("publish_mode", body.publish_mode)
        if body.photo_edit_mode is not None:
            if body.photo_edit_mode not in {"auto", "manual"}: raise HTTPException(status_code=400, detail="Некоректний режим фото")
            await set_setting("photo_edit_mode", body.photo_edit_mode)
        if body.processing_paused is not None:
            await set_setting("processing_paused", "true" if body.processing_paused else "false")
    return {"ok": True}


@app.get("/api/analytics")
async def analytics(user=Depends(current_user)):
    uid = int(user["id"])
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """WITH RECURSIVE dates(d) AS (
                   SELECT date('now','-6 day') UNION ALL SELECT date(d,'+1 day') FROM dates WHERE d < date('now')
               ) SELECT d, COUNT(n.id) FROM dates LEFT JOIN news n ON date(n.created_at)=d AND n.user_id=? GROUP BY d ORDER BY d""",
            (uid,),
        )
        days = [{"day": row[0], "total": int(row[1])} for row in await cur.fetchall()]
        cur = await db.execute(
            "SELECT source,COUNT(*) c FROM news WHERE user_id=? AND created_at>=datetime('now','-7 day') GROUP BY source ORDER BY c DESC LIMIT 8",
            (uid,),
        )
        sources = [{"source": row[0], "count": int(row[1])} for row in await cur.fetchall()]
    return {"days": days, "sources": sources}


@app.get("/api/media/{news_id}/{position}")
async def media_proxy(news_id: int, position: int, uid: int, original: int = 0, exp: int = 0, sig: str = ""):
    if exp < int(time.time()) or not hmac.compare_digest(sig, _sign_media(uid, news_id, position, bool(original), exp)):
        raise HTTPException(status_code=403, detail="Media link expired")
    if not is_authorized_id(uid): raise HTTPException(status_code=403, detail="Access denied")
    from app import album_support, main as app_main
    from app.database import get_news
    with user_scope(uid):
        row = await get_news(news_id)
        if not row: raise HTTPException(status_code=404, detail="Post not found")
        items = await album_support.get_news_media(news_id)
    file_id = None
    media_type = row.get("media_type") or "photo"
    if items:
        item = next((item for item in items if int(item.get("position") or 0) == int(position)), None)
        if not item: raise HTTPException(status_code=404, detail="Media not found")
        media_type = item.get("media_type") or "photo"
        file_id = item.get("original_file_id") if original else (item.get("edited_file_id") or item.get("original_file_id"))
    else:
        file_id = (row.get("original_media_file_id") if original else row.get("media_file_id")) or row.get("media_file_id")
    if not file_id: raise HTTPException(status_code=404, detail="Media not found")
    tg_file = await app_main.publisher.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    return Response(content=data, media_type="video/mp4" if media_type == "video" else "image/jpeg", headers={"Cache-Control": "private, max-age=600"})


if STATIC_DIR.exists():
    app.mount("/miniapp", StaticFiles(directory=str(STATIC_DIR), html=True), name="miniapp")


async def _serve() -> None:
    from app import album_support
    from app.content_policy import ensure_policy_schema
    from app.database import init_db
    await init_db()
    await ensure_policy_schema()
    await album_support.ensure_media_schema()
    port = int(os.getenv("PORT") or "8080")
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


async def start_miniapp_server() -> asyncio.Task:
    global _server_task
    if _server_task is None or _server_task.done():
        _server_task = asyncio.create_task(_serve(), name="auto-posting-miniapp")
        await asyncio.sleep(0)
        log.info("Mini App web server started public_url=%s", miniapp_public_url() or "not-configured")
    return _server_task
