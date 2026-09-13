import json
import logging
import re

from app.config import settings

log = logging.getLogger("telegram-ai-news.ukrainian-output")

_POLICY = """

МОВА ГОТОВОГО ПОСТА — ЖОРСТКО:
- Увесь текст, який побачить читач, має бути українською.
- Якщо джерело англійською або іншою мовою, переклади зміст природною українською.
- Прямі цитати та <blockquote> теж ОБОВ'ЯЗКОВО перекладай українською, зберігаючи зміст і тон.
- Не залишай цілі англомовні речення або фрагменти цитат у готовому пості.
- Дозволені іншомовні елементи тільки там, де це власна назва, ім'я, назва клубу/турніру/бренду або загальноприйнята абревіатура.
"""

_LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{4,}\b")


def _needs_second_pass(text: str, source: str) -> bool:
    if not str(source or "").startswith("web:"):
        return False
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return len(_LATIN_WORD_RE.findall(plain)) >= 6


async def _translate_final_html(text: str) -> str:
    from app import ai_editor
    prompt = (
        "Переклади цей ГОТОВИЙ Telegram-пост повністю українською. "
        "Збережи факти, імена, цифри та Telegram HTML <b>, <i>, <blockquote>, <tg-emoji>. "
        "Особливо переклади весь текст усередині <blockquote>. Не додавай фактів і не змінюй сенс. "
        "Власні назви, назви клубів/турнірів/брендів та загальновідомі абревіатури можна залишати оригінальними. "
        "Поверни тільки JSON {\"text\":\"...\"}.\n\nПОСТ:\n" + (text or "")[:6000]
    )
    response = await ai_editor.client.responses.create(
        model=settings.openai_model,
        input=[{"role": "user", "content": prompt}],
    )
    raw = (response.output_text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    data = json.loads(raw)
    return (data.get("text") or text).strip()


def install_ukrainian_output_policy() -> None:
    from app import ai_editor, admin_bot, main as main_mod

    if getattr(ai_editor, "_ukrainian_output_policy_installed", False):
        return
    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _POLICY
    original = ai_editor.rewrite_news

    async def rewrite_ua(text: str, source: str) -> dict:
        result = await original(text, source)
        if isinstance(result, dict) and result.get("publish") and result.get("text"):
            current = str(result["text"])
            if _needs_second_pass(current, source):
                try:
                    result["text"] = await _translate_final_html(current)
                except Exception:
                    log.exception("Ukrainian second-pass translation failed source=%s", source)
        return result

    ai_editor.rewrite_news = rewrite_ua
    main_mod.rewrite_news = rewrite_ua
    admin_bot.rewrite_news = rewrite_ua
    ai_editor._ukrainian_output_policy_installed = True
    log.info("Installed Ukrainian-only output policy with web quote translation")
