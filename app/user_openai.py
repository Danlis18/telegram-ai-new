import base64
import hashlib
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from openai import AsyncOpenAI

from app.config import settings
from app.database import get_setting, set_setting
from app.tenant import get_current_user_id, user_scope

log = logging.getLogger("telegram-ai-news.user-openai")

_KEY_SETTING = "openai_api_key_encrypted"
_HINT_SETTING = "openai_api_key_hint"


def _fernet() -> Fernet:
    # The database alone is not enough to recover user API keys. The runtime
    # Telegram bot token is mixed into the encryption key and remains in Railway env.
    material = (settings.telegram_bot_token + "|sports-news-user-openai-v1").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _mask(value: str) -> str:
    value = (value or "").strip()
    if len(value) <= 12:
        return "••••••••"
    return f"{value[:7]}…{value[-4:]}"


async def save_user_api_key(user_id: int, api_key: str) -> str:
    key = (api_key or "").strip()
    if len(key) < 20 or any(ch.isspace() for ch in key):
        raise ValueError("Некоректний OpenAI API key")
    token = _fernet().encrypt(key.encode("utf-8")).decode("ascii")
    hint = _mask(key)
    with user_scope(int(user_id)):
        await set_setting(_KEY_SETTING, token)
        await set_setting(_HINT_SETTING, hint)
    return hint


async def delete_user_api_key(user_id: int) -> None:
    with user_scope(int(user_id)):
        await set_setting(_KEY_SETTING, "")
        await set_setting(_HINT_SETTING, "")


async def _read_user_key(user_id: int | None = None) -> tuple[str | None, str]:
    uid = int(user_id) if user_id is not None else get_current_user_id()
    if uid is None:
        return None, ""
    with user_scope(int(uid)):
        encrypted = (await get_setting(_KEY_SETTING, "") or "").strip()
        hint = (await get_setting(_HINT_SETTING, "") or "").strip()
    if not encrypted:
        return None, hint
    try:
        value = _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8").strip()
        return value or None, hint
    except (InvalidToken, ValueError, UnicodeError):
        log.warning("Stored per-user OpenAI key could not be decrypted user_id=%s", uid)
        return None, hint


async def openai_key_status(user_id: int | None = None) -> dict:
    key, hint = await _read_user_key(user_id)
    if key:
        return {"source": "personal", "configured": True, "hint": hint or _mask(key)}
    global_key = (settings.openai_api_key or "").strip()
    return {
        "source": "system" if global_key else "none",
        "configured": bool(global_key),
        "hint": "Railway OPENAI_API_KEY" if global_key else "",
    }


async def get_effective_api_key(user_id: int | None = None) -> str:
    key, _ = await _read_user_key(user_id)
    if key:
        return key
    fallback = (settings.openai_api_key or "").strip()
    if not fallback:
        raise RuntimeError("OPENAI_API_KEY_NOT_CONFIGURED")
    return fallback


async def get_openai_client(user_id: int | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=await get_effective_api_key(user_id))


async def test_user_api_key(user_id: int) -> tuple[bool, str]:
    try:
        client = await get_openai_client(int(user_id))
        # Listing models is cheap and does not generate content or consume model tokens.
        await client.models.list()
        state = await openai_key_status(int(user_id))
        label = "персональний" if state["source"] == "personal" else "системний"
        return True, f"API працює · {label} ключ"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:240]}"


class _DynamicResponses:
    async def create(self, *args, **kwargs):
        client = await get_openai_client()
        return await client.responses.create(*args, **kwargs)


class _DynamicImages:
    async def generate(self, *args, **kwargs):
        client = await get_openai_client()
        return await client.images.generate(*args, **kwargs)

    async def edit(self, *args, **kwargs):
        client = await get_openai_client()
        return await client.images.edit(*args, **kwargs)


class DynamicOpenAIProxy:
    def __init__(self):
        self.responses = _DynamicResponses()
        self.images = _DynamicImages()


def install_per_user_openai() -> None:
    """Patch existing AI modules without disturbing their established logic."""
    proxy = DynamicOpenAIProxy()
    from app import ai_editor, content_policy

    ai_editor.client = proxy
    content_policy.client = proxy
    log.info("Installed encrypted per-user OpenAI client routing")
