import logging
from contextlib import suppress

from app.config import settings
from app.database import get_default_target
from app.user_publisher import _ready_client, _resolve_target

log = logging.getLogger("telegram-ai-news.channel-hygiene")

_TEST_MARKERS = (
    "перевірка premium emoji",
    "тест premium emoji",
    "якщо ти бачиш анімований емодзі",
    "якщо цей 🚀 відображається як premium emoji",
    "публікація premium працює",
)


async def remove_old_test_posts() -> None:
    """Delete only our old Premium/test diagnostics from publication channels.

    This function never sends messages. It only removes known diagnostic posts that
    were previously created during Premium publisher testing.
    """
    client = await _ready_client()
    if client is None:
        return

    targets: list[str] = []
    if settings.admin_user_id:
        with suppress(Exception):
            target = await get_default_target(int(settings.admin_user_id))
            if target and target.get("channel_ref"):
                targets.append(str(target["channel_ref"]))
    legacy = str(settings.target_channel or "").strip()
    if legacy and legacy not in targets:
        targets.append(legacy)

    for channel_ref in targets:
        try:
            entity = await _resolve_target(client, channel_ref)
            delete_ids: list[int] = []
            async for message in client.iter_messages(entity, limit=100):
                text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "").lower()
                if text and any(marker in text for marker in _TEST_MARKERS):
                    delete_ids.append(int(message.id))
            if delete_ids:
                await client.delete_messages(entity, delete_ids)
                log.info("Removed %s old Premium/test posts from %s", len(delete_ids), channel_ref)
        except Exception:
            log.exception("Could not clean old test posts from %s", channel_ref)
