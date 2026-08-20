import base64
import json
import re
from io import BytesIO

from openai import AsyncOpenAI
from PIL import Image

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


async def _source_needs_cleanup(source_image: bytes) -> bool:
    """Cheap vision gate: clean photos bypass the image-generation API entirely."""
    response = await client.responses.create(
        model=settings.openai_model,
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Inspect this sports image only to decide whether it needs CLEANUP. "
                        "Return exactly CLEAN or EDIT. EDIT only if there is clearly visible overlaid editorial/promotional content that should be removed: headline/caption text, score graphics, quote blocks, watermarks, channel/media logos, bookmaker/casino branding, advertising badges or CTA overlays. "
                        "Do NOT count normal real-world details as removable overlays: player shirt/kit manufacturer marks, club crests, competition patches, stadium signs that naturally belong to the photographed scene, jersey numbers, tattoos, or ordinary objects. "
                        "If it is already an ordinary clean sports photograph with no unwanted overlay, return CLEAN. When uncertain, return CLEAN so the original is not altered."
                    ),
                },
                {"type": "input_image", "image_url": _image_data_url(source_image)},
            ],
        }],
    )
    verdict = response.output_text.strip().upper()
    return verdict.startswith("EDIT")


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    """Keep clean source images byte-for-byte unchanged; edit only when cleanup is required."""
    clean_context = re.sub(r"<[^>]+>", " ", news_text)[:1200]

    if source_image:
        # Critical rule: do not spend an image-generation call and do not touch the
        # pixels if there is nothing unwanted to remove.
        if not await _source_needs_cleanup(source_image):
            return source_image

        prompt = (
            "THIS IS A SURGICAL CLEANUP OF THE PROVIDED IMAGE, NOT A REGENERATION. "
            "Keep the source image visually identical everywhere except the unwanted OVERLAID graphics that must be removed. "
            "Remove only clearly overlaid headline/caption text, score graphics, quote blocks, watermarks, channel/media logos, bookmaker/casino branding, advertising badges and CTA overlays. "
            "DO NOT remove or alter authentic parts of the photographed scene: the person's face, hair, body, pose, clothing, jersey design, club crest, kit manufacturer logo, competition patch, jersey number, tattoos, stadium, crowd, field, natural signage, lighting, shadows, crop, camera angle, depth of field or colors. "
            "Do not redesign, restyle, beautify, recolor, relight, sharpen, add a template, replace the person, change clothing, invent a background, or add any new text/logo. "
            "After removing an unwanted overlay, inpaint ONLY that removed area from the surrounding original pixels so it looks naturally empty. "
            "No SPORTS NEWS branding is added during cleanup. Preserve original composition and photographic realism. "
            f"Context only, never render it: {clean_context}"
        )
        image_file = BytesIO(source_image)
        image_file.name = "source.png"
        result = await client.images.edit(
            model=settings.openai_image_model,
            image=image_file,
            prompt=prompt,
            size="1536x1024",
        )
        item = result.data[0]
        if getattr(item, "b64_json", None):
            return base64.b64decode(item.b64_json)
        raise RuntimeError("Image API did not return image bytes")

    # Fallback only when a post genuinely has no source image.
    selected_key, template = get_template(template_key, news_text)
    prompt = (
        "Create a clean photorealistic sports editorial photograph, landscape 4:3. "
        "No text, no logos, no watermarks, no branding, no bookmaker/casino marks, no graphic poster layout. "
        "Use natural saturated photographic colors, realistic skin, anatomy, clothing, lighting and shadows. "
        "Avoid AI-art appearance and excessive effects. "
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
        raw = base64.b64decode(item.b64_json)
        return _to_four_three(raw)
    raise RuntimeError("Image API did not return image bytes")
