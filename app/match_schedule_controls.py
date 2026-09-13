import logging

from app.database import get_setting, list_users
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.match-controls")


def install_match_schedule_controls() -> None:
    from app import match_schedule

    if getattr(match_schedule, "_per_user_controls_installed", False):
        return
    original = match_schedule.publish_daily_fixtures_once

    async def controlled(user_id: int | None = None):
        if user_id is None:
            results = []
            for row in await list_users(active_only=True):
                uid = int(row.get("telegram_user_id") or 0)
                if not uid:
                    continue
                with user_scope(uid):
                    enabled = (await get_setting("daily_fixtures_enabled", "true") or "true").lower() == "true"
                if enabled:
                    results.append(await original(uid))
            return {"ok": True, "users": results}

        uid = int(user_id)
        with user_scope(uid):
            enabled = (await get_setting("daily_fixtures_enabled", "true") or "true").lower() == "true"
        if not enabled:
            return {"ok": True, "reason": "disabled"}
        return await original(uid)

    match_schedule.publish_daily_fixtures_once = controlled
    match_schedule._per_user_controls_installed = True
    log.info("Installed per-user daily fixture controls")
