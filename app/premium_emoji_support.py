import html
import re

from app import ai_editor

_CUSTOM_EMOJI_RE = re.compile(
    r'<tg-emoji\s+emoji-id=["\'](?P<id>\d+)["\']\s*>(?P<fallback>.*?)</tg-emoji>',
    re.IGNORECASE | re.DOTALL,
)

_PREMIUM_RULES = """

PREMIUM CUSTOM EMOJI — КРИТИЧНО ВАЖЛИВО:
- Джерело новини та ручні правки редактора можуть містити Telegram Premium emoji у форматі <tg-emoji emoji-id="123456789">🔥</tg-emoji>.
- Вважай такий тег ОДНИМ emoji, а не текстом, HTML-сміттям або службовою розміткою.
- Якщо у вихідній новині є Premium emoji, використовуй доречний Premium emoji з ДЖЕРЕЛА у готовому пості. Для першого хука віддавай перевагу Premium emoji з джерела замість звичайного emoji.
- Якщо у corrected_text редактора є Premium emoji, вивчай їх так само, як стиль тексту, і можеш повторно використовувати у наступних доречних новинах.
- Дозволено використовувати ТІЛЬКИ точні emoji-id, які вже присутні у поточному джерелі або переданих редакторських прикладах. Ніколи не вигадуй emoji-id.
- Не екрануй, не видаляй і не переписуй валідний <tg-emoji ...>...</tg-emoji> у звичайний текст.
- Premium emoji може стояти всередині <b>...</b>, так само як звичайний emoji.
- У першому абзаці Premium emoji рахується як той самий один дозволений emoji. Не став поруч ще один звичайний emoji.
- Поле text може містити Telegram HTML <b>, <i>, <blockquote> та <tg-emoji emoji-id="...">...</tg-emoji>. Markdown не використовуй.
"""


def _utf16_index(text: str, units: int) -> int:
    """Translate Telegram UTF-16 entity offsets to a Python string index."""
    if units <= 0:
        return 0
    used = 0
    for index, char in enumerate(text):
        size = len(char.encode("utf-16-le")) // 2
        if used + size > units:
            return index
        used += size
        if used == units:
            return index + 1
    return len(text)


def _fallback_emoji(value: str) -> str:
    """Telegram requires a real fallback emoji inside <tg-emoji>."""
    value = html.unescape((value or "").strip())
    if not value or value.startswith("<"):
        return "✨"
    return value[:16]


def telethon_message_html(message) -> str:
    """Preserve Telethon MessageEntityCustomEmoji as Bot API HTML tags."""
    text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "").strip()
    if not text:
        return ""

    entities = list(getattr(message, "entities", None) or [])
    custom = []
    for entity in entities:
        if type(entity).__name__ != "MessageEntityCustomEmoji":
            continue
        document_id = getattr(entity, "document_id", None)
        if not document_id:
            continue
        start = _utf16_index(text, int(getattr(entity, "offset", 0)))
        end = _utf16_index(
            text,
            int(getattr(entity, "offset", 0)) + int(getattr(entity, "length", 0)),
        )
        if start >= end:
            continue
        custom.append((start, end, str(document_id)))

    if not custom:
        return text

    rendered = text
    for start, end, emoji_id in sorted(custom, key=lambda item: item[0], reverse=True):
        fallback = _fallback_emoji(rendered[start:end])
        tag = f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        rendered = rendered[:start] + tag + rendered[end:]
    return rendered


def event_text_html(event) -> str:
    """Extract caption/text with custom emoji from a normal post or album."""
    messages = list(getattr(event, "album_messages", None) or [])
    carrier = getattr(event, "message", None)
    if carrier is not None and carrier not in messages:
        messages.insert(0, carrier)
    if not messages:
        messages = [event]

    for message in messages:
        raw = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "").strip()
        if raw:
            return telethon_message_html(message)
    return (getattr(event, "raw_text", None) or "").strip()


class _PremiumEventProxy:
    def __init__(self, event, raw_text: str):
        self._event = event
        self.raw_text = raw_text

    def __getattr__(self, name):
        return getattr(self._event, name)


def _ids_from_text(text: str) -> set[str]:
    return {match.group("id") for match in _CUSTOM_EMOJI_RE.finditer(text or "")}


def _normalize_ai_premium_emoji(text: str, allowed_ids: set[str]) -> str:
    """Keep known custom emoji IDs and downgrade hallucinated IDs to fallback emoji."""
    def repl(match: re.Match) -> str:
        emoji_id = match.group("id")
        fallback = _fallback_emoji(match.group("fallback"))
        if emoji_id not in allowed_ids:
            return fallback
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

    return _CUSTOM_EMOJI_RE.sub(repl, text or "")


async def _allowed_editor_emoji_ids() -> set[str]:
    allowed: set[str] = set()
    try:
        examples = await ai_editor.get_style_examples(12)
    except Exception:
        return allowed
    for example in examples:
        allowed.update(_ids_from_text(example.get("corrected_text") or ""))
    return allowed


def install_premium_emoji_support() -> None:
    if getattr(ai_editor, "_premium_emoji_support_installed", False):
        return

    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _PREMIUM_RULES

    original_rewrite = ai_editor.rewrite_news

    async def rewrite_news_with_premium_emoji(text: str, source: str) -> dict:
        allowed = _ids_from_text(text)
        allowed.update(await _allowed_editor_emoji_ids())
        result = await original_rewrite(text, source)
        if isinstance(result, dict) and result.get("text"):
            result["text"] = _normalize_ai_premium_emoji(str(result["text"]), allowed)
        return result

    ai_editor.rewrite_news = rewrite_news_with_premium_emoji

    # Telethon's raw_text omits the custom-emoji ID. Convert the reader's
    # MessageEntityCustomEmoji entities to Bot API HTML before AI sees them.
    from app import admin_bot, main as main_mod

    original_process = main_mod.process_message

    async def process_message_with_premium_emoji(event, source: str, user_id: int, **kwargs):
        rendered = event_text_html(event)
        if rendered:
            event = _PremiumEventProxy(event, rendered)
        return await original_process(event, source, user_id, **kwargs)

    main_mod.process_message = process_message_with_premium_emoji

    # These modules imported rewrite_news directly before this runtime patch.
    # Replace their bound references so normal parsing AND manual "Інший варіант"
    # use the same ID validation and learned Premium emoji palette.
    main_mod.rewrite_news = rewrite_news_with_premium_emoji
    admin_bot.rewrite_news = rewrite_news_with_premium_emoji

    ai_editor._premium_emoji_support_installed = True
