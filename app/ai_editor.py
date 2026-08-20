import base64
import json
import re
from openai import AsyncOpenAI
from app.config import settings

client = AsyncOpenAI(api_key=settings.openai_api_key)

BETKING_RE = re.compile(r"(?i)(bet\s*king|бет\s*кінг|беткінг)")
URL_RE = re.compile(r"(?i)(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)")
AD_TAG_RE = re.compile(r"(?i)(#реклама|#промо|#promo|#advertising|#ad\b)")

SYSTEM_PROMPT = """Ти редактор українського спортивного Telegram-каналу SPORTS NEWS.

Завдання: на основі вхідної новини створити самостійний природний Telegram-пост українською.

СТРУКТУРА ОБОВ'ЯЗКОВА:
1) Перший абзац — ОДНЕ природне речення, яке одразу захоплює увагу і чітко пояснює, про що новина.
2) Далі тіло новини — 1-3 короткі абзаци залежно від обсягу джерела. Не розтягуй текст. Орієнтуйся приблизно на довжину оригіналу, але роби його чистішим і читабельнішим.
3) Не додавай фінальний підпис каналу — його додасть код автоматично.

ПРАВИЛА:
- Не копіюй формулювання джерела дослівно.
- Не вигадуй фактів, цитат, рахунків, дат, сум чи деталей.
- Видаляй рекламу, промокоди, CTA, згадки про канал-джерело та будь-які згадки BetKing/Беткінг.
- Не вставляй зовнішні посилання.
- Якщо новина сумнівна, недостатньо підтверджена або виглядає рекламною — publish=false.
- Стиль живий, професійний, спортивний, без канцелярщини та без зайвої води.
- Зазвичай тримай текст у межах приблизно 250-900 символів.

Поверни тільки JSON:
{"publish":bool,"score":0-100,"text":"...","reason":"..."}
"""


def sanitize_source_text(text: str) -> str:
    # Special rule: BetKing is removed, not automatically treated as a reason to discard the news.
    cleaned = BETKING_RE.sub("", text)
    cleaned = re.sub(r"(?i)https?://\S*betking\S*", "", cleaned)
    cleaned = re.sub(r"(?i)t\.me/\S*betking\S*", "", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()


def is_advertising_post(text: str) -> tuple[bool, str]:
    if AD_TAG_RE.search(text):
        return True, "advertising hashtag"

    # Remove BetKing first because the user explicitly wants its mention deleted while keeping useful news.
    stripped = sanitize_source_text(text)
    if URL_RE.search(stripped):
        return True, "external link"

    return False, ""


async def rewrite_news(text: str, source: str) -> dict:
    clean_text = sanitize_source_text(text)
    response = await client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Джерело: @{source}\n\nНовина:\n{clean_text}"},
        ],
    )
    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    result = json.loads(raw)
    result["text"] = sanitize_source_text((result.get("text") or "").strip())
    return result


async def generate_news_image(news_text: str) -> bytes:
    prompt = (
        "Create a premium editorial sports news image for a Ukrainian Telegram sports channel. "
        "Use the news context below. No logos, no betting brands, no watermarks, no readable text, "
        "no fake scores or numbers. Clean modern sports-media aesthetic, realistic editorial composition, "
        "strong subject focus, suitable for a square Telegram post. News context: " + news_text[:1800]
    )
    result = await client.images.generate(
        model=settings.openai_image_model,
        prompt=prompt,
        size="1024x1024",
    )
    item = result.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    raise RuntimeError("Image API did not return image bytes")
