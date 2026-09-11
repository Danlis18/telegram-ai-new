import logging

import aiosqlite

from app.config import settings
from app.sources import SOURCES

log = logging.getLogger("telegram-ai-news.source-whitelist")

ALLOWED_SOURCES = tuple(dict.fromkeys(source.strip().lstrip("@").lower() for source in SOURCES if source.strip()))


async def _sync_persistent_sources() -> None:
    """Keep parser source storage restricted to the configured whitelist."""
    if not ALLOWED_SOURCES:
        return

    placeholders = ",".join("?" for _ in ALLOWED_SOURCES)
    async with aiosqlite.connect(settings.database_path) as db:
        # Remove old/default/custom sources that are no longer allowed so the
        # persistent Railway database matches the current parser whitelist.
        await db.execute(
            f"DELETE FROM user_sources WHERE lower(username) NOT IN ({placeholders})",
            ALLOWED_SOURCES,
        )

        # The owner should always have the complete selected source set active.
        if settings.admin_user_id:
            owner_id = int(settings.admin_user_id)
            for source in ALLOWED_SOURCES:
                await db.execute(
                    """INSERT INTO user_sources (user_id,username,is_active)
                       VALUES (?,?,1)
                       ON CONFLICT(user_id,username) DO UPDATE SET is_active=1""",
                    (owner_id, source),
                )

        await db.commit()

    log.info("Telegram source whitelist synchronized: %d sources", len(ALLOWED_SOURCES))


def install_source_whitelist() -> None:
    """Restrict source discovery to SOURCES even if stale DB rows still exist."""
    from app import main as main_mod

    if getattr(main_mod, "_source_whitelist_installed", False):
        return

    original_init_db = main_mod.init_db
    original_list_sources = main_mod.list_all_active_source_usernames
    allowed = set(ALLOWED_SOURCES)

    async def init_db_with_source_whitelist():
        await original_init_db()
        await _sync_persistent_sources()

    async def list_whitelisted_sources() -> list[str]:
        sources = await original_list_sources()
        filtered = [source for source in sources if source.strip().lstrip("@").lower() in allowed]
        return filtered

    main_mod.init_db = init_db_with_source_whitelist
    main_mod.list_all_active_source_usernames = list_whitelisted_sources
    main_mod._source_whitelist_installed = True
    log.info("Installed Telegram source whitelist with %d allowed channels", len(allowed))
