import asyncio
import logging

import aiosqlite
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.utils import get_peer_id

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
        await db.execute(
            f"DELETE FROM user_sources WHERE lower(username) NOT IN ({placeholders})",
            ALLOWED_SOURCES,
        )

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


async def _sync_all_whitelisted_sources(main_mod) -> tuple[int, list[str]]:
    """Resolve/join every allowed source; one FloodWait must not stop the rest."""
    wanted_sources = await main_mod.list_all_active_source_usernames()
    main_mod.ACTIVE_SOURCE_IDS.clear()

    joined_by_username: dict[str, tuple[int, str]] = {}
    async for dialog in main_mod.reader.iter_dialogs():
        entity = dialog.entity
        username = getattr(entity, "username", None)
        if username:
            joined_by_username[username.lower()] = (dialog.id, username)

    missing: list[str] = []
    joined_now = 0

    for source in wanted_sources:
        normalized = source.strip().lstrip("@").lower()
        cached = joined_by_username.get(normalized)
        if cached:
            chat_id, canonical = cached
            main_mod.ACTIVE_SOURCE_IDS[int(chat_id)] = canonical
            continue

        try:
            entity = await main_mod.reader.get_entity(source)
        except Exception:
            log.exception("Could not resolve source @%s", source)
            missing.append(source)
            continue

        joined = False
        for attempt in range(2):
            try:
                await main_mod.reader(JoinChannelRequest(entity))
                joined = True
                joined_now += 1
                log.info("Joined source @%s", source)
                await asyncio.sleep(3)
                break
            except UserAlreadyParticipantError:
                joined = True
                break
            except FloodWaitError as exc:
                wait_seconds = max(1, int(exc.seconds))
                log.warning(
                    "Telegram FloodWait while joining @%s: %ss (attempt %s/2)",
                    source,
                    wait_seconds,
                    attempt + 1,
                )
                if attempt == 0 and wait_seconds <= 60:
                    await asyncio.sleep(wait_seconds + 1)
                    continue
                break
            except Exception:
                log.exception("Could not join source @%s", source)
                break

        if not joined:
            missing.append(source)
            continue

        canonical = getattr(entity, "username", None) or source
        main_mod.ACTIVE_SOURCE_IDS[get_peer_id(entity)] = canonical

    resolved_names = {name.lower() for name in main_mod.ACTIVE_SOURCE_IDS.values()}
    for source in wanted_sources:
        if source.lower() not in resolved_names and source not in missing:
            missing.append(source)

    await main_mod.set_setting("active_sources", str(len(main_mod.ACTIVE_SOURCE_IDS)))
    await main_mod.set_setting("missing_sources", ",".join(missing))
    log.info(
        "Source audit complete: active=%d/%d, joined_now=%d, missing=%d",
        len(main_mod.ACTIVE_SOURCE_IDS),
        len(wanted_sources),
        joined_now,
        len(missing),
    )
    return joined_now, missing


def install_source_whitelist() -> None:
    """Restrict source discovery to SOURCES and retry every selected channel."""
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
        return [source for source in sources if source.strip().lstrip("@").lower() in allowed]

    async def sync_whitelisted_sources() -> tuple[int, list[str]]:
        return await _sync_all_whitelisted_sources(main_mod)

    main_mod.init_db = init_db_with_source_whitelist
    main_mod.list_all_active_source_usernames = list_whitelisted_sources
    main_mod.sync_sources = sync_whitelisted_sources
    main_mod._source_whitelist_installed = True
    log.info("Installed Telegram source whitelist with %d allowed channels", len(allowed))
