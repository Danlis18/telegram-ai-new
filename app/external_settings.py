from app.database import get_setting, set_setting
from app.tenant import user_scope


async def get_external_settings(user_id: int) -> dict:
    with user_scope(int(user_id)):
        enabled = (await get_setting("web_sources_enabled", "true") or "true").lower() == "true"
        creative = (await get_setting("web_generate_creative", "true") or "true").lower() == "true"
        raw_limit = await get_setting("web_daily_limit", "8")
        raw_score = await get_setting("web_min_score", "62")
    try:
        daily_limit = max(1, min(20, int(raw_limit or 8)))
    except Exception:
        daily_limit = 8
    try:
        min_score = max(30, min(100, int(raw_score or 62)))
    except Exception:
        min_score = 62
    return {
        "enabled": enabled,
        "generate_creative": creative,
        "daily_limit": daily_limit,
        "min_score": min_score,
    }


async def set_external_enabled(user_id: int, value: bool) -> bool:
    with user_scope(int(user_id)):
        await set_setting("web_sources_enabled", "true" if value else "false")
    return bool(value)


async def set_generate_creative(user_id: int, value: bool) -> bool:
    with user_scope(int(user_id)):
        await set_setting("web_generate_creative", "true" if value else "false")
    return bool(value)


async def set_daily_limit(user_id: int, value: int) -> int:
    value = max(1, min(20, int(value)))
    with user_scope(int(user_id)):
        await set_setting("web_daily_limit", str(value))
    return value


async def set_min_score(user_id: int, value: int) -> int:
    value = max(30, min(100, int(value)))
    with user_scope(int(user_id)):
        await set_setting("web_min_score", str(value))
    return value
