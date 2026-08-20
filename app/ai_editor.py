import base64
import json
import re
from io import BytesIO

from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.image_templates import DEFAULT_IMAGE_TEMPLATE, get_template

client = AsyncOpenAI(api_key=settings.openai_api_key)

BETKING_RE = re.compile(r"(?i)(bet\s*king|бет\s*кінг|беткінг)")
URL_RE = re.compile(r"(?i)(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)")
AD_TAG_RE = re.compile(r"(?i)(#реклама|#промо|#promo|#advertising|#ad\b)")

SYSTEM_PROMPT = """Ти редактор українського спортивного Telegram-каналу SPORTS NEWS.

Твоя задача — не механічно перефразувати джерело, а написати самостійний, живий і природний Telegram-пост українською на основі перевірених фактів із вхідного тексту.

ОБОВ'ЯЗКОВА СТРУКТУРА:
1) ХУК — перший абзац.
- Починай з ОДНОГО доречного emoji.
- Після emoji — одне відносно коротке речення, яке передає головну суть і чіпляє увагу.
- Речення має комфортно поміщатися приблизно в 1-2 рядки Telegram; орієнтир — до ~100 символів, якщо зміст дозволяє.
- Увесь перший рядок/речення роби жирним через Telegram HTML: <b>...</b>.
- Не роби клікбейт заради клікбейту: хук має бути природним і фактичним.

2) ТІЛО НОВИНИ.
- Після хука — порожній рядок.
- Далі 1-3 короткі абзаци залежно від обсягу та важливості новини.
- Орієнтуйся приблизно на інформаційний обсяг оригіналу: коротку новину не роздувай, важливу не обрізай до одного рядка.
- Після одного з абзаців доречно використай ще ОДНЕ emoji як природний акцент. Не став його механічно в однаковому місці в кожному пості.
- Можеш інколи виділити 1-3 ключові слова через <b>...</b> або <i>...</i>, якщо це реально покращує читання.
- Якщо в джерелі є сильна коротка цитата — можеш природно використати її або винести в окремий короткий абзац. Не вигадуй цитат.

3) ФІНАЛ.
- Не додавай підпис SPORTS NEWS самостійно — його додасть код автоматично.

СТИЛЬ І ВІЗУАЛЬНИЙ РИТМ:
- Пиши так, ніби пост підготував живий спортивний редактор, а не AI.
- Дозволено мислити абстрактно у подачі: змінюй ритм, формулювання, спосіб входу в новину та акценти залежно від контексту.
- Форматування має бути стриманим: зазвичай 1 основне bold-виділення в хуку + максимум 1 додатковий короткий акцент у тілі.
- Не перетворюй текст на набір жирного, курсиву, emoji та декоративних символів.
- Не використовуй довгі лінії, підкреслення, розділювачі типу _____, =====, —— або декоративні блоки.
- Не роби шаблонні заголовки на кшталт «BREAKING», «ТЕРМІНОВО» чи «ОФІЦІЙНО», якщо це прямо не випливає з новини.
- Emoji мають відповідати змісту. Не використовуй однаковий emoji в кожному пості.
- Мова — природна сучасна українська, спортивна, впевнена, без канцеляризмів і зайвої води.

ФАКТИ ТА МОДЕРАЦІЯ:
- Не копіюй формулювання джерела дослівно.
- Не вигадуй фактів, цитат, рахунків, дат, сум, причин або деталей.
- Видаляй рекламу, промокоди, CTA, згадки про канал-джерело та будь-які згадки BetKing/Беткінг.
- Не вставляй зовнішні посилання.
- Якщо новина сумнівна, недостатньо підтверджена або виглядає рекламною — publish=false.
- Зазвичай тримай готовий текст приблизно в межах 250-900 символів, але зміст важливіший за механічний ліміт.

ВАЖЛИВО ПРО ФОРМАТ:
- Поле text повинно містити Telegram HTML (<b>, <i>) там, де потрібне форматування.
- Не використовуй Markdown **, __ або _ для оформлення.
- Використовуй тільки прості теги <b>...</b> та <i>...</i>.

Поверни тільки JSON:
{"publish":bool,"score":0-100,"text":"...","reason":"..."}
"""


def sanitize_source_text(text: str) -> str:
    cleaned = BETKING_RE.sub("", text)
    cleaned = re.sub(r"(?i)https?://\S*betking\S*", "", cleaned)
    cleaned = re.sub(r"(?i)t\.me/\S*betking\S*", "", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()


def is_advertising_post(text: str) -> tuple[bool, str]:
    if AD_TAG_RE.search(text):
        return True, "advertising hashtag"

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


def _add_sports_news_brand(image_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")

    band_h = max(72, int(height * 0.085))
    y0 = height - band_h
    draw.rectangle((0, y0, width, height), fill=(8, 10, 14, 210))
    draw.rectangle((0, y0, max(8, int(width * 0.012)), height), fill=(255, 255, 255, 235))

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(26, int(height * 0.035)))
        small = ImageFont.truetype("DejaVuSans.ttf", max(14, int(height * 0.018)))
    except OSError:
        font = ImageFont.load_default()
        small = ImageFont.load_default()

    x = max(28, int(width * 0.035))
    y = y0 + max(14, int(band_h * 0.18))
    draw.text((x, y), "SPORTS NEWS", font=font, fill=(255, 255, 255, 255))
    draw.text((x, y + max(31, int(height * 0.038))), "sports_news_ua", font=small, fill=(205, 210, 220, 255))

    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def generate_news_image(
    news_text: str,
    *,
    source_image: bytes | None = None,
    template_key: str = DEFAULT_IMAGE_TEMPLATE,
) -> bytes:
    template = get_template(template_key)
    common = (
        "Create a premium SPORTS NEWS editorial visual based on the news context and the selected layout direction. "
        "The final image must be square 1:1, minimal, modern, polished and suitable for a serious Ukrainian sports Telegram channel. "
        "ABSOLUTELY REMOVE or avoid every sponsor logo, betting brand, watermark, media logo, source-channel logo and unrelated brand mark. "
        "Do not generate readable headlines, fake scores, fake stats, fake quotes, numbers, team crests or sponsor marks. "
        "Keep the real athlete/coach recognizable when a reference image is provided, but recompose the visual into an original branded editorial layout. "
        "Use clean negative space, controlled contrast, subtle texture, no visual clutter. "
        f"Template direction: {template['prompt']} "
        f"News context: {news_text[:1800]}"
    )

    if source_image:
        image_file = BytesIO(source_image)
        image_file.name = "source.png"
        result = await client.images.edit(
            model=settings.openai_image_model,
            image=image_file,
            prompt=common,
            size="1024x1024",
        )
    else:
        result = await client.images.generate(
            model=settings.openai_image_model,
            prompt=common,
            size="1024x1024",
        )

    item = result.data[0]
    if getattr(item, "b64_json", None):
        raw = base64.b64decode(item.b64_json)
        return _add_sports_news_brand(raw)
    raise RuntimeError("Image API did not return image bytes")
