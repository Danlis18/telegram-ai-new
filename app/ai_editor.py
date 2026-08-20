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
- Заборонено робити пару типу: «NAVI вилетіли з EWC!» → «NAVI та MongolZ вибули з EWC». Це дубль, а не розвиток новини.
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


def _extract_visual_headline(news_text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", news_text or "") if p.strip()]
    headline = parts[0] if parts else news_text
    headline = re.sub(r"<[^>]+>", "", headline)
    headline = re.sub(r"^[^\wА-Яа-яІіЇїЄєҐґ]+", "", headline).strip()
    headline = headline.rstrip(".! ")
    if len(headline) > 82:
        cut = headline[:82].rsplit(" ", 1)[0]
        headline = cut if len(cut) > 35 else headline[:82]
    return headline.upper()


def _wrap_headline(text: str, max_chars: int = 23) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
            if len(lines) == 2:
                break
        else:
            current = candidate
    if current and len(lines) < 3:
        lines.append(current)
    return lines[:3]


def _add_editorial_overlay(image_bytes: bytes, news_text: str) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")

    # One and only one SPORTS NEWS lockup, always top-left.
    x = int(width * 0.045)
    y = int(height * 0.055)
    box = int(height * 0.092)
    try:
        sn_font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(height * 0.052))
        brand_font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(height * 0.031))
        headline_font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(height * 0.064))
    except OSError:
        sn_font = ImageFont.load_default()
        brand_font = ImageFont.load_default()
        headline_font = ImageFont.load_default()

    gold = (224, 177, 45, 255)
    white = (248, 248, 245, 255)
    draw.rectangle((x, y, x + box, y + box), outline=gold, width=3)
    sn_box = draw.textbbox((0, 0), "SN", font=sn_font)
    sn_w = sn_box[2] - sn_box[0]
    sn_h = sn_box[3] - sn_box[1]
    draw.text((x + (box - sn_w) / 2, y + (box - sn_h) / 2 - 5), "SN", font=sn_font, fill=gold)
    draw.text((x + box + 18, y + 3), "SPORTS", font=brand_font, fill=white)
    draw.text((x + box + 18, y + int(box * 0.48)), "NEWS", font=brand_font, fill=white)

    # Professional headline only. No body text, quotes, microcopy or duplicate branding.
    headline = _extract_visual_headline(news_text)
    lines = _wrap_headline(headline)
    hx = x
    hy = int(height * 0.56)
    line_gap = int(height * 0.012)

    # Subtle local shadow/gradient panel keeps copy readable without looking like a box.
    panel_bottom = min(height - 30, hy + len(lines) * int(height * 0.085) + 42)
    draw.rounded_rectangle(
        (hx - 18, hy - 18, int(width * 0.47), panel_bottom),
        radius=14,
        fill=(5, 7, 10, 132),
    )
    draw.rectangle((hx - 18, hy - 18, hx - 10, panel_bottom), fill=gold)

    for idx, line in enumerate(lines):
        yy = hy + idx * (int(height * 0.072) + line_gap)
        # Realistic soft typographic shadow, then crisp white type.
        draw.text((hx + 3, yy + 4), line, font=headline_font, fill=(0, 0, 0, 150))
        draw.text((hx, yy), line, font=headline_font, fill=white)

    out = BytesIO()
    image.save(out, format="JPEG", quality=95, optimize=True)
    return out.getvalue()


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    selected_key, template = get_template(template_key, news_text)
    clean_context = re.sub(r"<[^>]+>", " ", news_text)[:1800]
    common = (
        "Create a premium photorealistic football/sports editorial background for a Ukrainian news channel. "
        "LANDSCAPE 4:3. The visual must feel rich, energetic and human art-directed like a top European sports-media graphic, not like AI art. "
        "Use deep charcoal/black, warm metallic gold and controlled white highlights. Increase visual richness with cinematic stadium atmosphere, realistic directional light, strong but natural contrast, realistic contact shadows, rim light, depth separation, subtle haze, fine film grain, fabric detail and believable skin texture. "
        "The image must be vivid and dynamic, NOT flat, dull, washed-out, empty or lifeless. Use layered depth, foreground/background separation and confident sports-editorial geometry while remaining clean and premium. "
        "CRITICAL LAYOUT: reserve the LEFT 43% as clean negative space for professional headline typography that will be added later by code. Keep the primary athlete/coach mostly on the RIGHT 52-58% and do not place important faces or body details in the left headline zone. "
        f"Selected thematic template: {selected_key}. {template['prompt']} "
        "DO NOT render any text at all. No headline, no captions, no quotes, no tiny labels, no score text, no statistics, no numbers, no watermarks. "
        "DO NOT render SPORTS NEWS, SN or any channel logo anywhere; branding is added exactly once later by code. "
        "Avoid/remove all betting brands, sponsor marks, source-channel logos and unrelated media branding. Do not invent club crests or sponsor logos. "
        "If a reference/source photo is supplied, preserve the real person's identity, facial structure and photographic realism; recompose the reference into the editorial scene instead of inventing a different person. "
        "Realistic anatomy only: correct face, eyes, hands, fingers, jersey folds, perspective and shadows. No plastic skin, distorted logos, surreal shapes, excessive glow or fake graphic artifacts. "
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
        return _add_editorial_overlay(_to_four_three(raw), news_text)
    raise RuntimeError("Image API did not return image bytes")
