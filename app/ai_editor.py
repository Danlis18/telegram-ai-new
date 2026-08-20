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
- Обирай emoji тематично під конкретну новину, а не один і той самий постійно: трансфер, матч, травма, заява, рекорд, сенсація тощо.
- Увесь emoji разом із хуком обов'язково оформлюй <b>...</b>.
- НІКОЛИ не став крапку в кінці першого речення.
- У кінці хука дозволено або знак оклику !, або взагалі без кінцевого знака.
- У першому абзаці не використовуй більше одного emoji.

2. Другий абзац — перший змістовний абзац тіла новини.
- ВІН НЕ МАЄ ПОВТОРЮВАТИ ЗАГОЛОВОК іншими словами.
- Якщо хук уже повідомив факт «хто + що сталося», другий абзац повинен одразу додати НОВУ інформацію: деталі, умови, контекст, причину, наслідок, рахунок, суперника, суму, цитату або інший факт із джерела.
- Заборонено робити пару типу: «NAVІ вилетіли з EWC!» → «NAVI та MongolZ вибули з EWC». Це дубль, а не розвиток новини.
- Основна частина повинна РОЗКРИВАТИ хук, а не переказувати його.
- Саме тут, якщо це природно, можна використати ще ОДИН доречний emoji.
- Emoji у другому абзаці став тільки в кінці повного речення.
- Не став emoji всередині речення, після окремого слова, після двокрапки, у середині цитати або одразу після закриття цитати.
- Якщо другий emoji не пасує — не став його взагалі.

3. Далі — ще 0-2 короткі абзаци залежно від обсягу джерела. Не роздувай коротку новину і не обрізай важливу.
- Кожен наступний абзац має додавати новий зміст, а не повторювати попередній.
- Додаткові emoji зазвичай не потрібні.
- Можна стримано виділити 1-3 ключові слова через <b> або <i>, якщо це реально допомагає читанню.

4. Якщо джерело містить реальну пряму мову, заяву, коментар або дослівну цитату людини — найважливішу цитату оформлюй нативним Telegram HTML: <blockquote>...</blockquote>.
- Не роби blockquote зі звичайного переказу.
- Не вигадуй, не дописуй і не прикрашай цитату.
- Emoji не став у кінці blockquote.

5. SPORTS NEWS в кінці не додавай — це робить код.

ГОЛОВНЕ РЕДАКТОРСЬКЕ ПРАВИЛО:
Хук = коротко повідомляє головну подію.
Тіло = відповідає «що саме сталося далі / які деталі / чому це важливо».
Якщо в джерелі недостатньо фактів для нормального тіла, краще зроби коротший пост з одним змістовним абзацом, ніж повторюй заголовок.

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
            "Перепиши пост так, щоб другий абзац одразу давав нову деталь із джерела. "
            "Не повторюй ті самі команди/гравців/подію тим самим формулюванням без додаткового факту."
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


def _brand_font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _add_sports_news_shield(image_bytes: bytes) -> bytes:
    """Add exactly one deterministic SN SPORTS shield at top-left.

    The image model never draws brand text. This overlay is rendered by PIL,
    so the logo is always readable, consistent and never duplicated.
    """
    image = Image.open(BytesIO(image_bytes)).convert("RGBA")
    width, height = image.size

    logo_w = int(width * 0.145)
    logo_h = int(logo_w * 1.12)
    margin_x = int(width * 0.035)
    margin_y = int(height * 0.04)

    logo = Image.new("RGBA", (logo_w, logo_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo, "RGBA")

    # Shield silhouette based on the approved black/gold/white SN SPORTS mark.
    outer = [
        (int(logo_w * 0.08), int(logo_h * 0.08)),
        (int(logo_w * 0.50), int(logo_h * 0.00)),
        (int(logo_w * 0.92), int(logo_h * 0.08)),
        (int(logo_w * 0.88), int(logo_h * 0.70)),
        (int(logo_w * 0.50), int(logo_h * 0.98)),
        (int(logo_w * 0.12), int(logo_h * 0.70)),
    ]
    d.polygon(outer, fill=(8, 10, 13, 235), outline=(222, 176, 42, 255))

    inner = [
        (int(logo_w * 0.15), int(logo_h * 0.13)),
        (int(logo_w * 0.50), int(logo_h * 0.065)),
        (int(logo_w * 0.85), int(logo_h * 0.13)),
        (int(logo_w * 0.82), int(logo_h * 0.67)),
        (int(logo_w * 0.50), int(logo_h * 0.90)),
        (int(logo_w * 0.18), int(logo_h * 0.67)),
    ]
    d.line(inner + [inner[0]], fill=(245, 245, 242, 220), width=max(2, logo_w // 70), joint="curve")

    sn_font = _brand_font(int(logo_h * 0.30))
    sports_font = _brand_font(int(logo_h * 0.115))

    # SN monogram: white S + gold N.
    sn_y = int(logo_h * 0.18)
    s_box = d.textbbox((0, 0), "S", font=sn_font)
    n_box = d.textbbox((0, 0), "N", font=sn_font)
    sw = s_box[2] - s_box[0]
    nw = n_box[2] - n_box[0]
    total = sw + nw - int(logo_w * 0.05)
    sx = (logo_w - total) // 2
    d.text((sx, sn_y), "S", font=sn_font, fill=(248, 248, 245, 255))
    d.text((sx + sw - int(logo_w * 0.05), sn_y), "N", font=sn_font, fill=(224, 177, 45, 255))

    sports = "SPORTS"
    tb = d.textbbox((0, 0), sports, font=sports_font)
    tw = tb[2] - tb[0]
    d.text(((logo_w - tw) // 2, int(logo_h * 0.54)), sports, font=sports_font, fill=(248, 248, 245, 255))

    # Small speed stripes from the approved crest language.
    y0 = int(logo_h * 0.70)
    for i, scale in enumerate((0.56, 0.46, 0.36)):
        line_w = int(logo_w * scale)
        x0 = (logo_w - line_w) // 2
        color = (224, 177, 45, 255) if i != 1 else (248, 248, 245, 255)
        d.rounded_rectangle((x0, y0 + i * int(logo_h * 0.055), x0 + line_w, y0 + i * int(logo_h * 0.055) + max(3, logo_h // 55)), radius=3, fill=color)

    # Soft shadow so the mark stays readable on bright photos without a visible box.
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.alpha_composite(logo, (margin_x + 4, margin_y + 6))
    shadow_alpha = shadow.getchannel("A").point(lambda a: min(120, a // 2))
    shadow.putalpha(shadow_alpha)
    image.alpha_composite(shadow)
    image.alpha_composite(logo, (margin_x, margin_y))

    out = BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=95, optimize=True)
    return out.getvalue()


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    selected_key, template = get_template(template_key, news_text)
    clean_context = re.sub(r"<[^>]+>", " ", news_text)[:1800]

    common = (
        "Create a premium photorealistic football/sports editorial image for a Ukrainian sports news channel. "
        "LANDSCAPE 4:3. The result must look like a real professional sports-media photo/composite made by a human designer, not AI art. "
        "COLOR RULE: preserve REAL, natural, saturated colors of skin, hair, uniforms, grass, stadium lights and environment. "
        "Never apply a global gold, yellow, sepia, orange or monochrome color grade. Gold/white/black are branding accents only and must NOT tint the photograph. "
        "Use realistic white balance, natural skin tones, authentic club colors, strong clean contrast, true blacks, detailed highlights and believable shadows. "
        "Make the image vivid and energetic with realistic stadium/editorial lighting, depth separation, subtle haze where appropriate, fine photographic grain, fabric detail and natural skin texture. "
        "Avoid plastic skin, over-sharpening, muddy brown shadows, fake glow, surreal geometry, distorted anatomy, fake fingers, fake logos or obviously generated faces. "
        f"Selected thematic composition: {selected_key}. {template['prompt']} "
        "IMPORTANT: DO NOT render any text, headline, caption, quote, score, number, tiny label, watermark, SPORTS NEWS, SN or channel logo. "
        "The code adds exactly one approved SN SPORTS shield later in the TOP-LEFT corner. Keep that corner reasonably clean. "
        "Remove/avoid betting brands, casino marks, bookmaker sponsors, source-channel logos and unrelated media watermarks. "
        "If a source image is supplied, preserve the real person's identity and the useful sports content. If it is already a designed sports collage, clean/recompose it rather than inventing a completely different scene. "
        f"News context: {clean_context}"
    )

    if source_image:
        image_file = BytesIO(source_image)
        image_file.name = "source.png"
        result = await client.images.edit(
            model=settings.openai_image_model,
            image=image_file,
            prompt=common,
            size="1536x1024",
        )
    else:
        result = await client.images.generate(
            model=settings.openai_image_model,
            prompt=common,
            size="1536x1024",
        )

    item = result.data[0]
    if getattr(item, "b64_json", None):
        raw = base64.b64decode(item.b64_json)
        return _add_sports_news_shield(_to_four_three(raw))
    raise RuntimeError("Image API did not return image bytes")
