from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import get_setting, set_setting
from app.miniapp_server import app, current_user
from app.tenant import user_scope
from app.user_ai_runtime import api_key_status, delete_user_api_key, set_user_api_key

_installed = False


class ExternalSettingsBody(BaseModel):
    web_enabled: bool | None = None
    daily_fixtures_enabled: bool | None = None
    web_min_score: int | None = Field(default=None, ge=40, le=95)
    web_max_per_day: int | None = Field(default=None, ge=1, le=20)


class ApiKeyBody(BaseModel):
    api_key: str = Field(min_length=20, max_length=300)


async def _payload(uid: int) -> dict:
    with user_scope(uid):
        web_enabled = (await get_setting("web_sources_enabled", "true") or "true").lower() == "true"
        fixtures_enabled = (await get_setting("daily_fixtures_enabled", "true") or "true").lower() == "true"
        min_score = int(await get_setting("web_min_score", "62") or 62)
        max_daily = int(await get_setting("web_max_per_day", "8") or 8)
    key = await api_key_status(uid)
    return {
        "web_enabled": web_enabled,
        "daily_fixtures_enabled": fixtures_enabled,
        "web_min_score": min_score,
        "web_max_per_day": max_daily,
        "openai": key,
    }


def install_external_control_api() -> None:
    global _installed
    if _installed:
        return

    @app.get("/api/external-control")
    async def external_control(user=Depends(current_user)):
        return await _payload(int(user["id"]))

    @app.patch("/api/external-control")
    async def update_external_control(body: ExternalSettingsBody, user=Depends(current_user)):
        uid = int(user["id"])
        with user_scope(uid):
            if body.web_enabled is not None:
                await set_setting("web_sources_enabled", "true" if body.web_enabled else "false")
            if body.daily_fixtures_enabled is not None:
                await set_setting("daily_fixtures_enabled", "true" if body.daily_fixtures_enabled else "false")
            if body.web_min_score is not None:
                await set_setting("web_min_score", str(int(body.web_min_score)))
            if body.web_max_per_day is not None:
                await set_setting("web_max_per_day", str(int(body.web_max_per_day)))
        return await _payload(uid)

    @app.put("/api/openai-key")
    async def put_openai_key(body: ApiKeyBody, user=Depends(current_user)):
        uid = int(user["id"])
        try:
            await set_user_api_key(uid, body.api_key, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return await _payload(uid)

    @app.delete("/api/openai-key")
    async def remove_openai_key(user=Depends(current_user)):
        uid = int(user["id"])
        await delete_user_api_key(uid)
        return await _payload(uid)

    _installed = True
