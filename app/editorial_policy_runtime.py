import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings

log = logging.getLogger("telegram-ai-news.editorial-policy")

_RUSSIA_RE = re.compile(
    r"(?iu)\b(росі(?:я|ї|єю|ю)|російськ\w*|рф\b|russia\b|russian\w*|"
    r"москв\w*|збірн\w*\s+росі\w*|россия|российск\w*|сборн\w*\s+росси\w*)"
)

_TIMEZONE_RE = re.compile(
    r"(?iu)\b(мск|msk|мск\.|московськ(?:ий|им|ого)\s+час(?:ом|у)?|"
    r"московск(?:ое|ому|ого)\s+врем(?:я|ени)|utc(?:[+-]\d{1,2})?|gmt(?:[+-]\d{1,2})?|"
    r"cet|cest|eet|eest|est|edt|cst|cdt|mst|mdt|pst|pdt)\b"
)

_POLICY_PROMPT = """

ЧАС У НОВИНАХ — ОБОВ'ЯЗКОВО:
- Канал український. Усі години з будь-яких чужих часових поясів переводь у локальний час Europe/Kyiv.
- Якщо джерело пише МСК/MSK, UTC/GMT, CET/CEST, EET/EEST, EST/EDT, PST/PDT або інший часовий пояс — перерахуй саму годину на Київ.
- Якщо в джерелі є дата події, враховуй саме цю дату при переході літній/зимовий час у Europe/Kyiv.
- Якщо дати немає, орієнтуйся на поточний київський час зі службового контексту, доданого до вхідної новини.
- У готовому пості НЕ пиши «МСК», «за московським часом», «за Києвом», «за українським часом», UTC, GMT або назву будь-якого часового поясу. Просто пиши природно: «початок о 22:00».
- Не залишай чужу годину без конвертації.

РЕДАКЦІЙНИЙ ГЕОФІЛЬТР — АБСОЛЮТНИЙ:
- Канал орієнтується на спорт Європи та Америки.
- БУДЬ-ЯКИЙ матеріал про російські ліги, клуби, збірні, спортсменів, функціонерів або турніри в Росії — publish=false без винятків.
- Не публікуй російський спорт навіть у негативному, критичному, санкційному чи скандальному контексті.
- Якщо Росія/російський спортивний суб'єкт є суттєвою частиною новини — publish=false.
- Не намагайся переписувати російську тему так, щоб вона пройшла фільтр.
- Пріоритет контенту: топові ліги, клуби, збірні та турніри Європи, США, Канади, Мексики, Південної та Центральної Америки.
- Пиши фактологічно, без образ чи мови ненависті.
"""


def _kyiv_context() -> str:
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    offset = now.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "Europe/Kyiv"
    return (
        "\n\n[СЛУЖБОВИЙ ЧАСОВИЙ КОНТЕКСТ — НЕ ПУБЛІКУВАТИ]\n"
        f"Зараз у Europe/Kyiv: {now.strftime('%Y-%m-%d %H:%M')} ({offset}).\n"
        "Цей рядок потрібен тільки для правильного перерахунку часу. Не цитуй і не згадуй його в пості."
    )


async def _russia_context_allowed(source_text: str) -> tuple[bool, str]:
    """Absolute hard gate: Russia-related sports content is never publishable."""
    if _RUSSIA_RE.search(source_text or ""):
        return False, "Russia-related sports content is disabled"
    return True, "not Russia-related"


def _strip_timezone_labels(text: str) -> str:
    """Safety cleanup: model should convert first; this only removes leftover labels from final copy."""
    value = text or ""
    value = re.sub(r"(?iu)\s*\(?\s*(?:за\s+)?(?:московським|московским)\s+часом\s*\)?", "", value)
    value = re.sub(r"(?iu)\s*\(?\s*(?:мск|msk|utc(?:[+-]\d{1,2})?|gmt(?:[+-]\d{1,2})?|cet|cest|eet|eest|est|edt|pst|pdt)\s*\)?", "", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def install_editorial_policy() -> None:
    from app import ai_editor, admin_bot, main as main_mod

    if getattr(ai_editor, "_sports_news_editorial_policy_installed", False):
        return

    # Append persistent editorial rules after Premium emoji rules.
    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _POLICY_PROMPT
    original_rewrite = ai_editor.rewrite_news

    async def rewrite_with_editorial_policy(text: str, source: str) -> dict:
        # Add current Kyiv offset to this request only; do not mutate the global prompt,
        # because several source posts can be rewritten concurrently.
        request_text = (text or "") + _kyiv_context()
        result = await original_rewrite(request_text, source)

        if not isinstance(result, dict):
            return result

        if result.get("text"):
            result["text"] = _strip_timezone_labels(str(result["text"]))

        # Absolute hard gate: Russia-related sports stories never reach the ready queue.
        if result.get("publish") and _RUSSIA_RE.search(text or ""):
            allowed, reason = await _russia_context_allowed(text)
            if not allowed:
                result["publish"] = False
                result["score"] = min(int(result.get("score") or 0), 20)
                result["reason"] = f"Russia editorial filter: {reason}"
                result["text"] = ""

        # If a timezone label somehow survived, suppress it from copy rather than expose foreign timezone notation.
        if result.get("text") and _TIMEZONE_RE.search(str(result["text"])):
            result["text"] = _strip_timezone_labels(str(result["text"]))
        return result

    ai_editor.rewrite_news = rewrite_with_editorial_policy
    main_mod.rewrite_news = rewrite_with_editorial_policy
    admin_bot.rewrite_news = rewrite_with_editorial_policy
    ai_editor._sports_news_editorial_policy_installed = True
    log.info("Installed Kyiv-time normalization and Russia editorial publication filter")
