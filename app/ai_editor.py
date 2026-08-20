import base64
import json
import re
from io import BytesIO

from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.database import get_style_examples
from app.image_templates import DEFAULT_IMAGE_TEMPLATE, get_template

client = AsyncOpenAI(api_key=settings.openai_api_key)

BETKING_RE = re.compile(r"(?i)(bet\s*king|бет\s*кінг|беткінг)")
URL_RE = re.compile(r"(?i)(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)")
AD_TAG_RE = re.compile(r"(?i)(#реклама|#промо|#promo|#advertising|#ad\b)")

SYSTEM_PROMPT = """Ти редактор українського спортивного Telegram-каналу SPORTS NEWS.
Пиши самостійний, живий Telegram-пост українською тільки з фактів джерела.

СТРУКТУРА ТА ПУНКТУАЦІЯ:
1. Перший абзац — одне коротке речення-хук приблизно до 100 символів і максимум 1-2 рядки Telegram.
- Увесь хук обов'язково оформлюй <b>...</b>.
- НІКОЛИ не став крапку в кінці першого речення.
- У кінці хука дозволено або знак оклику !, або взагалі без кінцевого знака.
- Не став emoji у першому абзаці.

2. Другий абзац — перший змістовний абзац тіла новини.
- Саме тут, якщо це природно, можна використати ОДИН доречний emoji.
- Emoji став тільки в кінці повного речення.
- Не став emoji всередині речення, після окремого слова, після двокрапки, у середині цитати або одразу після закриття цитати.
- Якщо emoji не пасує — не став його взагалі.

3. Далі — ще 0-2 короткі абзаци залежно від обсягу джерела. Не роздувай коротку новину і не обрізай важливу.
- Додаткові emoji зазвичай не потрібні.
- Можна стримано виділити 1-3 ключові слова через <b> або <i>, якщо це реально допомагає читанню.

4. Якщо джерело містить реальну пряму мову, заяву, коментар або дослівну цитату людини — найважливішу цитату оформлюй нативним Telegram HTML: <blockquote>...</blockquote>.
- Не роби blockquote зі звичайного переказу.
- Не вигадуй, не дописуй і не прикрашай цитату.
- Emoji не став у кінці blockquote.

5. SPORTS NEWS в кінці не додавай — це робить код.

СТИЛЬ:
Жива сучасна українська, спортивна й природна. Без AI-канцеляризмів, води, довгих розділювачів, декоративного сміття і штучного клікбейту. Варіюй ритм та конструкції, щоб пости не виглядали шаблонними. Форматування стримане і функціональне.

МОДЕРАЦІЯ:
Не вигадуй фактів, цитат, рахунків, дат, сум чи причин. Видаляй рекламу, CTA, промокоди, згадки джерела і BetKing/Беткінг. Не вставляй зовнішні посилання. Сумнівне або рекламне: publish=false.

НАВЧАННЯ НА ПРАВКАХ РЕДАКТОРА:
Якщо нижче передані приклади ручних виправлень редактора, вважай corrected_text еталоном стилю. Аналізуй, що редактор змінив порівняно з ai_text, і повторюй ці закономірності в нових постах. Не копіюй факти з прикладів у нову новину — переймай лише стиль, ритм, форматування та редакторські звички.

ФОРМАТ:
Поле text може містити тільки Telegram HTML <b>, <i>, <blockquote>. Не використовуй Markdown.
Поверни тільки JSON: {"publish":bool,"score":0-100,"text":"...","reason":"..."}
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


def _feedback_context(examples: list[dict]) -> str:
    if not examples:
        return ""
    chunks = ["\n\nОСТАННІ РУЧНІ ПРАВКИ РЕДАКТОРА (corrected_text = еталон):"]
    for idx, example in enumerate(reversed(examples), 1):
        ai_text = (example.get("ai_text") or "")[:900]
        corrected = (example.get("corrected_text") or "")[:900]
        chunks.append(f"\nПриклад {idx}:\nAI:\n{ai_text}\nРЕДАКТОР:\n{corrected}")
    return "\n".join(chunks)


async def rewrite_news(text: str, source: str) -> dict:
    clean_text = sanitize_source_text(text)
    examples = await get_style_examples(6)
    response = await client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT + _feedback_context(examples)},
            {"role": "user", "content": f"Джерело: @{source}\n\nНовина:\n{clean_text}"},
        ],
    )
    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    result = json.loads(raw)
    result["text"] = sanitize_source_text((result.get("text") or "").strip())
    return result


def _to_four_three(image_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    target_ratio = 4 / 3
    ratio = image.width / image.height
    if ratio > target_ratio:
        new_w = int(image.height * target_ratio)
        left = (image.width - new_w) // 2
        image = image.crop((left, 0, left + new_w, image.height))
    elif ratio < target_ratio:
        new_h = int(image.width / target_ratio)
        top = (image.height - new_h) // 2
        image = image.crop((0, top, image.width, top + new_h))
    image = image.resize((1200, 900), Image.Resampling.LANCZOS)
    out = BytesIO()
    image.save(out, format="JPEG", quality=94, optimize=True)
    return out.getvalue()


def _add_sports_news_brand(image_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    plate_w, plate_h = int(width * 0.22), int(height * 0.105)
    x0, y0 = int(width * 0.035), int(height * 0.84)
    draw.rounded_rectangle((x0, y0, x0 + plate_w, y0 + plate_h), radius=12, fill=(5, 7, 10, 205), outline=(214, 170, 45, 180), width=2)
    try:
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", int(height * 0.045))
        small = ImageFont.truetype("DejaVuSans-Bold.ttf", int(height * 0.018))
    except OSError:
        big = ImageFont.load_default(); small = ImageFont.load_default()
    draw.text((x0 + 16, y0 + 8), "SN", font=big, fill=(226, 183, 54, 255))
    draw.text((x0 + 82, y0 + 25), "SPORTS NEWS", font=small, fill=(250, 250, 248, 255))
    out = BytesIO(); image.save(out, format="JPEG", quality=94, optimize=True)
    return out.getvalue()


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    selected_key, template = get_template(template_key, news_text)
    common = (
        "Create a photorealistic premium Ukrainian football/sports news editorial card. "
        "Use a LANDSCAPE 4:3 composition. This belongs to one consistent SPORTS NEWS visual system: deep charcoal/black base, restrained metallic-gold accents, crisp white typography zones, cinematic stadium/editorial lighting, realistic contact shadows, rim light, depth, subtle grain and believable photographic texture. "
        "It must look human art-directed, not AI-generated: realistic anatomy, face, hands, jersey fabric, hair, skin, perspective and shadows; no plastic skin, surreal geometry, fake details or excessive glow. "
        "Keep the family resemblance of the six approved templates, but vary crop, subject position, diagonals, background depth and accent placement so consecutive cards are not clones. "
        f"Selected thematic template: {selected_key}. {template['prompt']} "
        "SPORTS NEWS branding must be clearly recognizable as an SN + SPORTS NEWS lockup, integrated into a safe corner or clean negative-space area. It should be noticeable but secondary to the news, never cover a face, player, score or headline. "
        "Avoid/remove all betting brands, sponsor marks, source-channel logos, watermarks and unrelated media branding. Do not invent team crests, sponsor logos, scores, statistics or quotes. "
        "If a reference/source photo is supplied, preserve the real person's identity and photographic realism and redesign/recompose that photo into this template instead of inventing a different person. "
        "Keep any generated headline extremely short and only when confidently supported by the supplied news; otherwise reserve clean headline space rather than hallucinating text. "
        f"News context: {re.sub(r'<[^>]+>', ' ', news_text)[:1800]}"
    )
    if source_image:
        image_file = BytesIO(source_image); image_file.name = "source.png"
        result = await client.images.edit(model=settings.openai_image_model, image=image_file, prompt=common, size="1536x1024")
    else:
        result = await client.images.generate(model=settings.openai_image_model, prompt=common, size="1536x1024")
    item = result.data[0]
    if getattr(item, "b64_json", None):
        raw = base64.b64decode(item.b64_json)
        return _add_sports_news_brand(_to_four_three(raw))
    raise RuntimeError("Image API did not return image bytes")
