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
- Якщо дати немає, орієнтуйся на поточний київський час, який передається нижче в системному контексті.
- У готовому пості НЕ пиши «МСК», «за московським часом», «за Києвом», «за українським часом», UTC, GMT або назву будь-якого часового поясу. Просто пиши природно: «початок о 22:00».
- Не залишай чужу годину без конвертації.

РЕДАКЦІЙНИЙ ФІЛЬТР ЩОДО РОСІЇ — ЖОРСТКО:
- Якщо новина головним чином просуває, хвалить, популяризує або нейтрально висвітлює Росію, російську збірну, клуб, спортсмена, функціонера чи іншу російську спортивну сторону — publish=false.
- Не публікуй перемоги, рекорди, успіхи, трансфери, досягнення, позитивні цитати, анонси чи звичайне нейтральне висвітлення російських спортивних суб'єктів.
- Дозволяй публікацію лише коли сам факт джерела має явно негативний/критичний для російської сторони контекст: поразка, провал, дискваліфікація, санкції, покарання, викриття порушення, скандал, агресія/неправомірна дія, інший очевидно негативний наслідок.
- НІКОЛИ не перекручуй нейтральну або позитивну новину в негативну лише для проходження фільтра. Якщо джерело саме не дає негативного факту — publish=false.
- Пиши фактологічно, без образ, мови ненависті чи приниження людей за національністю.
"""


def _kyiv_context() -> str:
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    offset = now.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "Europe/Kyiv"
    return (
        "\n\nПОТОЧНИЙ ЧАСОВИЙ КОНТЕКСТ ДЛЯ КОНВЕРТАЦІЇ:\n"
        f"Зараз у Europe/Kyiv: {now.strftime('%Y-%m-%d %H:%M')} ({offset}).\n"
        "Це службовий контекст. Не згадуй його в готовому пості."
    )


async def _russia_context_allowed(source_text: str) -> tuple[bool, str]:
    """Second-pass gate only for Russia-related stories; avoids accidental positive/neutral publication."""
    if not _RUSSIA_RE.search(source_text or ""):
        return True, "not Russia-related"

    from app import ai_editor

    prompt = """Ти виконуєш лише редакційний фільтр для українського спортивного каналу.
Проаналізуй ТІЛЬКИ факти джерела.
ALLOW=true тільки якщо головний російський суб'єкт у цій новині перебуває в явно негативному/критичному контексті: поразка, провал, санкція, дискваліфікація, покарання, викрите порушення, скандал, неправомірна дія/агресія або інший очевидно негативний для нього наслідок.
ALLOW=false для позитивної або нейтральної новини: перемога, успіх, рекорд, трансфер, досягнення, позитивна цитата, анонс, звичайна участь/результат без негативного контексту.
Не домислюй негатив. Не оцінюй людей за національністю.
Поверни тільки JSON: {"allow":bool,"reason":"коротко"}."""
    try:
        response = await ai_editor.client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": source_text[:6000]},
            ],
        )
        raw = response.output_text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        return bool(data.get("allow")), str(data.get("reason") or "Russia editorial filter")
    except Exception as exc:
        # Fail closed for Russia-related content: better skip one item than publish prohibited context.
        log.exception("Russia editorial classifier failed")
        return False, f"Russia filter error: {type(exc).__name__}"


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

    # Append rules to whatever prompt is currently active (including Premium emoji rules).
    ai_editor.SYSTEM_PROMPT = ai_editor.SYSTEM_PROMPT.rstrip() + _POLICY_PROMPT
    original_rewrite = ai_editor.rewrite_news

    async def rewrite_with_editorial_policy(text: str, source: str) -> dict:
        # Dynamic Kyiv offset is included on every request so undated foreign times can be converted correctly.
        original_prompt = ai_editor.SYSTEM_PROMPT
        ai_editor.SYSTEM_PROMPT = original_prompt + _kyiv_context()
        try:
            result = await original_rewrite(text, source)
        finally:
            ai_editor.SYSTEM_PROMPT = original_prompt

        if not isinstance(result, dict):
            return result

        if result.get("text"):
            result["text"] = _strip_timezone_labels(str(result["text"]))

        # Hard second pass: Russia-related positive/neutral stories never reach the ready queue.
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
