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
1. Перший абзац — ОДИН тематичний emoji + одне коротке речення-хук приблизно до 100 символів і максимум 1-2 рядки Telegram.
- Emoji ОБОВ'ЯЗКОВО став на самому початку, перед першим словом: <b>🔥 Текст хука!</b>
- Обирай emoji тематично під конкретну новину.
- Увесь emoji разом із хуком обов'язково оформлюй <b>...</b>.
- НІКОЛИ не став крапку в кінці першого речення.
- У кінці хука дозволено або знак оклику !, або взагалі без кінцевого знака.
- У першому абзаці не використовуй більше одного emoji.

2. Другий абзац — перший змістовний абзац тіла новини.
- ВІН НЕ МАЄ ПОВТОРЮВАТИ ЗАГОЛОВОК іншими словами.
- Якщо хук уже повідомив факт «хто + що сталося», другий абзац повинен одразу додати НОВУ інформацію: деталі, умови, контекст, причину, наслідок, рахунок, суперника, суму, цитату або інший факт із джерела.
- Основна частина повинна РОЗКРИВАТИ хук, а не переказувати його.
- Саме тут, якщо це природно, можна використати ще ОДИН доречний emoji.
- Emoji у другому абзаці став тільки в кінці повного речення.
- Якщо другий emoji не пасує — не став його взагалі.

3. Далі — ще 0-2 короткі абзаци залежно від обсягу джерела. Не роздувай коротку новину і не обрізай важливу.
- Кожен наступний абзац має додавати новий зміст, а не повторювати попередній.
- Можна стримано виділити 1-3 ключові слова через <b> або <i>.

4. Якщо джерело містить реальну пряму мову, заяву, коментар або дослівну цитату людини — найважливішу цитату оформлюй нативним Telegram HTML: <blockquote>...</blockquote>.
- Не роби blockquote зі звичайного переказу.
- Не вигадуй цитату.
- Emoji не став у кінці blockquote.

5. SPORTS NEWS в кінці не додавай — це робить код.

ГОЛОВНЕ РЕДАКТОРСЬКЕ ПРАВИЛО:
Хук = коротко повідомляє головну подію.
Тіло = відповідає «що саме сталося далі / які деталі / чому це важливо».
Якщо в джерелі недостатньо фактів, краще зроби коротший пост, ніж повторюй заголовок.

СТИЛЬ:
Жива сучасна українська, спортивна й природна. Без AI-канцеляризмів, води, довгих розділювачів, декоративного сміття і штучного клікбейту.

МОДЕРАЦІЯ:
Не вигадуй фактів, цитат, рахунків, дат, сум чи причин. Видаляй рекламу, CTA, промокоди, згадки джерела і BetKing/Беткінг. Не вставляй зовнішні посилання. Сумнівне або рекламне: publish=false.

НАВЧАННЯ НА ПРАВКАХ РЕДАКТОРА:
Якщо нижче передані приклади ручних виправлень редактора, вважай corrected_text еталоном стилю. Аналізуй зміни й повторюй ці закономірності в нових постах, не копіюючи факти.

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


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[^\wа-яіїєґА-ЯІЇЄҐ']+", " ", text, flags=re.UNICODE)
    return " ".join(text.lower().split())


def _headline_body_overlap(text: str) -> float:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(parts) < 2:
        return 0.0
    headline_words = {w for w in _plain(parts[0]).split() if len(w) > 2}
    body_words = {w for w in _plain(parts[1]).split() if len(w) > 2}
    if not headline_words or not body_words:
        return 0.0
    return len(headline_words & body_words) / max(1, min(len(headline_words), len(body_words)))


async def _request_rewrite(clean_text: str, source: str, examples: list[dict], extra_instruction: str = "") -> dict:
    response = await client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT + _feedback_context(examples) + extra_instruction},
            {"role": "user", "content": f"Джерело: @{source}\n\nНовина:\n{clean_text}"},
        ],
    )
    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    return json.loads(raw)


async def rewrite_news(text: str, source: str) -> dict:
    clean_text = sanitize_source_text(text)
    examples = await get_style_examples(6)
    result = await _request_rewrite(clean_text, source, examples)
    result["text"] = sanitize_source_text((result.get("text") or "").strip())
    if result.get("publish") and _headline_body_overlap(result["text"]) >= 0.62:
        retry_note = (
            "\n\nВАЖЛИВА ПОВТОРНА ПРАВКА: попередній варіант повторив зміст хука у другому абзаці. "
            "Перепиши пост так, щоб другий абзац одразу давав нову деталь із джерела."
        )
        retry = await _request_rewrite(clean_text, source, examples, retry_note)
        retry["text"] = sanitize_source_text((retry.get("text") or "").strip())
        result = retry
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
    image.save(out, format="JPEG", quality=95, optimize=True)
    return out.getvalue()


def _image_data_url(image_bytes: bytes) -> str:
    try:
        fmt = (Image.open(BytesIO(image_bytes)).format or "JPEG").lower()
    except Exception:
        fmt = "jpeg"
    if fmt == "jpg":
        fmt = "jpeg"
    return f"data:image/{fmt};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _add_sports_news_logo(image_bytes: bytes) -> bytes:
    """Add one compact SPORTS NEWS shield in the top-left without touching the rest."""
    image = Image.open(BytesIO(image_bytes)).convert("RGBA")
    width, height = image.size
    base = min(width, height)
    logo_w = max(92, int(base * 0.16))
    logo_h = int(logo_w * 0.92)
    margin = max(14, int(base * 0.025))

    logo = Image.new("RGBA", (logo_w, logo_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo, "RGBA")
    gold = (224, 177, 45, 255)
    white = (248, 248, 245, 255)
    dark = (7, 10, 14, 222)

    shield = [
        (int(logo_w * 0.08), int(logo_h * 0.08)),
        (int(logo_w * 0.50), 0),
        (int(logo_w * 0.92), int(logo_h * 0.08)),
        (int(logo_w * 0.88), int(logo_h * 0.68)),
        (int(logo_w * 0.50), int(logo_h * 0.98)),
        (int(logo_w * 0.12), int(logo_h * 0.68)),
    ]
    d.polygon(shield, fill=dark, outline=gold)

    sn_font = _font(int(logo_h * 0.29))
    sports_font = _font(int(logo_h * 0.105))
    small_font = _font(int(logo_h * 0.072))

    s_box = d.textbbox((0, 0), "S", font=sn_font)
    n_box = d.textbbox((0, 0), "N", font=sn_font)
    sw = s_box[2] - s_box[0]
    nw = n_box[2] - n_box[0]
    total = sw + nw - int(logo_w * 0.04)
    sx = (logo_w - total) // 2
    sy = int(logo_h * 0.13)
    d.text((sx, sy), "S", font=sn_font, fill=white)
    d.text((sx + sw - int(logo_w * 0.04), sy), "N", font=sn_font, fill=gold)

    label = "SPORTS"
    box = d.textbbox((0, 0), label, font=sports_font)
    d.text(((logo_w - (box[2] - box[0])) // 2, int(logo_h * 0.51)), label, font=sports_font, fill=white)
    news = "NEWS"
    box2 = d.textbbox((0, 0), news, font=small_font)
    d.text(((logo_w - (box2[2] - box2[0])) // 2, int(logo_h * 0.66)), news, font=small_font, fill=gold)

    image.alpha_composite(logo, (margin, margin))
    out = BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=96, optimize=True)
    return out.getvalue()


async def _source_needs_cleanup(source_image: bytes) -> bool:
    """Only foreign branding/logos trigger an expensive image edit."""
    response = await client.responses.create(
        model=settings.openai_model,
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Inspect this sports image. Return exactly CLEAN or EDIT. "
                        "EDIT ONLY when there is a clearly visible FOREIGN overlaid brand/logo/watermark that should be removed: another Telegram/media channel logo or name, bookmaker logo, casino logo, betting partner mark, commercial promotional watermark, or unrelated media watermark. "
                        "DO NOT mark EDIT merely because the image contains normal editorial text. Preserve headlines, captions, quotes, scores, dates, player names, match information, lists, numbers, arrows, graphic shapes and useful sports design. "
                        "Also preserve club/team crests, league/tournament logos, national federation marks, jersey numbers, kit manufacturer marks, sponsors physically printed on the real player's clothing, stadium signs, tattoos and all natural scene details. "
                        "If no foreign overlaid branding exists, return CLEAN. When uncertain, return CLEAN."
                    ),
                },
                {"type": "input_image", "image_url": _image_data_url(source_image)},
            ],
        }],
    )
    return response.output_text.strip().upper().startswith("EDIT")


def _restore_source_dimensions(edited_bytes: bytes, source_image: bytes) -> bytes:
    source = Image.open(BytesIO(source_image))
    edited = Image.open(BytesIO(edited_bytes)).convert("RGB")
    if edited.size != source.size:
        edited = edited.resize(source.size, Image.Resampling.LANCZOS)
    out = BytesIO()
    edited.save(out, format="JPEG", quality=96, optimize=True)
    return out.getvalue()


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    """Preserve original image; remove only foreign branding; always add SPORTS NEWS logo."""
    clean_context = re.sub(r"<[^>]+>", " ", news_text)[:1000]

    if source_image:
        working = source_image
        if await _source_needs_cleanup(source_image):
            prompt = (
                "SURGICAL EDIT OF THIS EXACT SOURCE IMAGE. Do NOT regenerate, redesign, restyle, recolor, relight, recompose or replace anything. "
                "REMOVE ONLY foreign overlaid brands/logos/watermarks: other Telegram/media channel branding, bookmaker/casino/betting branding, unrelated commercial promotional logos or watermarks. "
                "KEEP EVERY OTHER PIXEL/CONTENT AS CLOSE TO THE SOURCE AS POSSIBLE. In particular KEEP ALL useful existing text exactly visible and complete: headlines, captions, player names, quotes, scores, dates, lists, numbers, match information and graphic layout. "
                "KEEP sports identity elements: club/team crests, league/tournament/federation logos, kit manufacturer marks, jersey numbers and sponsors that are physically part of the photographed clothing or stadium scene. "
                "KEEP the same person, face, body, pose, clothing, crop, camera angle, background, colors, lighting, shadows and composition. "
                "Do not add any new text or branding. Only inpaint the tiny areas where a removable foreign logo/watermark was located. "
                f"Context only: {clean_context}"
            )
            image_file = BytesIO(source_image)
            image_file.name = "source.png"
            result = await client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
            )
            item = result.data[0]
            if not getattr(item, "b64_json", None):
                raise RuntimeError("Image API did not return image bytes")
            working = _restore_source_dimensions(base64.b64decode(item.b64_json), source_image)

        return _add_sports_news_logo(working)

    selected_key, template = get_template(template_key, news_text)
    prompt = (
        "Create a clean photorealistic sports editorial photograph. "
        "No foreign media/channel/bookmaker/casino branding. No artificial text. "
        "Natural saturated colors, realistic skin, anatomy, clothing, lighting and shadows. "
        f"Composition reference: {selected_key}. {template['prompt']} "
        f"News context: {clean_context}"
    )
    result = await client.images.generate(
        model=settings.openai_image_model,
        prompt=prompt,
        size="1536x1024",
    )
    item = result.data[0]
    if getattr(item, "b64_json", None):
        return _add_sports_news_logo(_to_four_three(base64.b64decode(item.b64_json)))
    raise RuntimeError("Image API did not return image bytes")
