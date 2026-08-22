import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.content_policy import (
    candidate_matches_preferences,
    can_accept_candidate,
    current_period,
    finish_replacement_request,
    list_pending_replacements,
)
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.reader-policy")
_user_locks: dict[int, asyncio.Lock] = {}


def _main_module():
    mod = sys.modules.get("app.main")
    if mod and hasattr(mod, "process_message"):
        return mod
    mod = sys.modules.get("__main__")
    if mod and hasattr(mod, "process_message") and hasattr(mod, "reader"):
        return mod
    return None


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(int(user_id))
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[int(user_id)] = lock
    return lock


async def _policy_process(original, event, source: str, user_id: int, **kwargs):
    uid = int(user_id)
    async with _lock_for(uid):
        with user_scope(uid):
            allowed, period_key, count, quota = await can_accept_candidate(uid)
            if not allowed:
                log.info(
                    "Quota gate skipped @%s user=%s period=%s count=%s quota=%s",
                    source,
                    uid,
                    period_key,
                    count,
                    quota,
                )
                return

            message = getattr(event, "message", event)
            if not getattr(message, "photo", None):
                return await original(event, source, uid, **kwargs)
            text = (getattr(event, "raw_text", None) or getattr(message, "raw_text", None) or "").strip()
            if len(text) >= 25:
                keep, reason = await candidate_matches_preferences(text, source, uid)
                if not keep:
                    stored_original = text or f"[PHOTO_ONLY] @{source} #{getattr(event, 'id', 0)}"
                    try:
                        if not await sys.modules[original.__module__].seen(stored_original):
                            await sys.modules[original.__module__].save(
                                source,
                                getattr(event, "id", 0),
                                stored_original,
                                "",
                                0,
                                "rejected",
                            )
                    except Exception:
                        log.exception("Could not persist learned preference rejection")
                    log.info("Learned preference rejected @%s user=%s: %s", source, uid, reason)
                    return

            return await original(event, source, uid, **kwargs)


async def _find_replacement(main_mod, request: dict) -> bool:
    uid = int(request["user_id"])
    requested_period = request.get("period_key")
    if current_period() != requested_period:
        await finish_replacement_request(request["id"], "expired")
        return True

    with user_scope(uid):
        allowed, _, before_count, quota = await can_accept_candidate(uid)
    if not allowed:
        await finish_replacement_request(request["id"], "fulfilled")
        return True

    cutoff = datetime.now(timezone.utc) - timedelta(hours=18)
    sources = list(main_mod.ACTIVE_SOURCE_IDS.items())
    for chat_id, source in sources:
        try:
            subscribers = await main_mod.get_source_subscribers(source)
            if uid not in subscribers:
                continue
            messages = await main_mod.reader.get_messages(chat_id, limit=12)
            for message in messages:
                if not getattr(message, "photo", None):
                    continue
                if message.date and message.date < cutoff:
                    continue
                stored = (message.raw_text or "").strip() or f"[PHOTO_ONLY] @{source} #{message.id}"
                with user_scope(uid):
                    if await main_mod.seen(stored):
                        continue
                await main_mod.process_message(message, source, uid, backfill=True, push_ready=True)
                with user_scope(uid):
                    allowed_after, _, after_count, _ = await can_accept_candidate(uid)
                if after_count > before_count or not allowed_after or after_count >= quota:
                    await finish_replacement_request(request["id"], "fulfilled")
                    log.info("Replacement fulfilled request=%s user=%s with @%s #%s", request["id"], uid, source, message.id)
                    return True
                await asyncio.sleep(0.15)
        except Exception:
            log.exception("Replacement scan failed source=@%s user=%s", source, uid)
    return False


async def replacement_worker() -> None:
    while True:
        try:
            await asyncio.sleep(20)
            main_mod = _main_module()
            if not main_mod or not getattr(main_mod, "ACTIVE_SOURCE_IDS", None):
                continue
            requests = await list_pending_replacements(20)
            for request in requests:
                await _find_replacement(main_mod, request)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Replacement worker iteration failed")
        await asyncio.sleep(40)


def install_reader_policy(app) -> None:
    main_mod = _main_module()
    if main_mod and not getattr(main_mod, "_quota_policy_wrapped", False):
        original = main_mod.process_message

        async def wrapped_process_message(event, source: str, user_id: int, **kwargs):
            return await _policy_process(original, event, source, user_id, **kwargs)

        main_mod.process_message = wrapped_process_message
        main_mod._quota_policy_wrapped = True
        log.info("Installed per-user quota and rejection-learning reader policy")

    old = app.bot_data.get("replacement_worker_task")
    if not old or old.done():
        try:
            app.bot_data["replacement_worker_task"] = asyncio.create_task(replacement_worker())
        except RuntimeError:
            pass
