import re

from app import ai_editor

_PREMIUM_RULES = """

PREMIUM CUSTOM EMOJI:
- Ручні правки редактора можуть містити Telegram Premium emoji у форматі <tg-emoji emoji-id="123456789">🔥</tg-emoji>.
- Вважай такий тег одним звичайним emoji, а не текстом/HTML-сміттям.
- Якщо у corrected_text є Premium emoji, вивчай їх так само, як стиль тексту, і використовуй у наступних доречних новинах.
- Дозволено повторно використовувати ТІЛЬКИ точні emoji-id, які вже є в переданих редакторських прикладах. Ніколи не вигадуй emoji-id.
- Не екрануй, не видаляй і не переписуй валідний <tg-emoji ...>...</tg-emoji> у звичайний текст.
- Premium emoji може стояти всередині <b>...</b>, так само як звичайний emoji.
- Поле text може містити Telegram HTML <b>, <i>, <blockquote> та <tg-emoji emoji-id="...">...</tg-emoji>. Markdown не використовуй.
"""


def install_premium_emoji_support() -> None:
    if getattr(ai_editor, "_premium_emoji_support_installed", False):
        return

    # The base prompt historically said that only b/i/blockquote were allowed.
    # Append a later, explicit rule so learned editor examples with tg-emoji are
    # treated as valid formatting and can be reused with their exact IDs.
    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _PREMIUM_RULES
    ai_editor._premium_emoji_support_installed = True
