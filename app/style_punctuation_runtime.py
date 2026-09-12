import logging

log = logging.getLogger("telegram-ai-news.style-punctuation")

_STYLE_RULES = """

ПУНКТУАЦІЯ ТА ТИРЕ:
- Використовуй тире помірно. Не будуй кожен абзац або друге речення через «—».
- У короткому Telegram-пості зазвичай достатньо 0-1 тире, якщо воно справді природне за змістом.
- Де можливо без втрати природності, замінюй тире на кому, двокрапку, крапку або нормальний сполучник.
- Не роби стиль шаблонним конструкціями на кшталт «Команда — зробила», «Матч — відбудеться», якщо тире граматично не потрібне.
- Тире можна залишати в доречних конструкціях, прямій мові, поясненні або протиставленні. Повністю забороняти його не треба.
- Не змінюй через це спортивні рахунки, назви чи інші фактичні позначення з джерела.
"""


def install_punctuation_style() -> None:
    """Reduce repetitive dash-heavy AI prose without banning natural punctuation."""
    from app import ai_editor

    if getattr(ai_editor, "_sports_news_punctuation_style_installed", False):
        return

    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _STYLE_RULES
    ai_editor._sports_news_punctuation_style_installed = True
    log.info("Installed moderate dash usage rule for SPORTS NEWS rewrites")
