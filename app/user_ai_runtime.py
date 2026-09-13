import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from openai import AsyncOpenAI

from app.config import settings
from app.database import get_setting, set_setting
from app.tenant import get_current_user_id, user_scope

log = logging.getLogger("telegram-ai-news.user-ai")
_KEY_SETTING = "openai_api_key_encrypted"


def _fernet() -> Fernet:
    seed = hashlib.sha256(("auto-posting:user-ai:" + settings.telegram_bot_token).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(seed))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str | None:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        return None


async def get_user_api_key(user_id: int | None = None) -> str | None:
    uid = int(user_id or get_current_user_id() or 0)
    if not uid:
        return None
    with user_scope(uid):
        encrypted = (await get_setting(_KEY_SETTING, "") or "").strip()
    return _decrypt(encrypted) if encrypted else None


async def set_user_api_key(user_id: int, api_key: str, *, validate: bool = True) -> None:
    key = (api_key or "").strip()
    if len(key) < 20 or not key.startswith("sk-"):
        raise ValueError("Схоже, це не OpenAI API key")
    if validate:
        probe = AsyncOpenAI(api_key=key, timeout=20)
        try:
            await probe.models.list()
        except Exception as exc:
            raise ValueError(f"OpenAI не прийняв цей API key: {type(exc).__name__}") from exc
    with user_scope(int(user_id)):
        await set_setting(_KEY_SETTING, _encrypt(key))
    _client_for_key.cache_clear()


async def delete_user_api_key(user_id: int) -> None:
    with user_scope(int(user_id)):
        await set_setting(_KEY_SETTING, "")
    _client_for_key.cache_clear()


async def api_key_status(user_id: int) -> dict:
    key = await get_user_api_key(int(user_id))
    if key:
        tail = key[-4:] if len(key) >= 4 else "••••"
        return {"custom": True, "masked": f"••••••••{tail}", "source": "user"}
    return {"custom": False, "masked": "Railway / shared", "source": "global"}


@lru_cache(maxsize=64)
def _client_for_key(key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=key)


async def current_ai_client() -> AsyncOpenAI:
    key = await get_user_api_key()
    return _client_for_key(key or settings.openai_api_key)


class _ResponsesProxy:
    async def create(self, *args, **kwargs):
        client = await current_ai_client()
        return await client.responses.create(*args, **kwargs)


class _ImagesProxy:
    async def generate(self, *args, **kwargs):
        client = await current_ai_client()
        return await client.images.generate(*args, **kwargs)

    async def edit(self, *args, **kwargs):
        client = await current_ai_client()
        return await client.images.edit(*args, **kwargs)


class ContextualOpenAI:
    def __init__(self):
        self.responses = _ResponsesProxy()
        self.images = _ImagesProxy()


contextual_client = ContextualOpenAI()


def install_user_ai_runtime() -> None:
    from app import ai_editor, admin_bot, content_policy

    ai_editor.client = contextual_client
    content_policy.client = contextual_client
    admin_bot.image_debug_client = contextual_client
    log.info("Installed per-user OpenAI client routing")
