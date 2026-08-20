import base64
import json
import logging
import re
from io import BytesIO
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from PIL import Image, ImageFile, UnidentifiedImageError

from app.config import settings
from app.database import get_style_examples
from app.image_templates import DEFAULT_IMAGE_TEMPLATE, get_template

ImageFile.LOAD_TRUNCATED_IMAGES = True
log = logging.getLogger("telegram-ai-news.ai-editor")
client = AsyncOpenAI(api_key=settings.openai_api_key)

BETKING_RE = re.compile(r"(?i)(bet\s*king|бет\s*кінг|беткінг)")
URL_RE = re.compile(r"(?i)(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)")
AD_TAG_RE = re.compile(r"(?i)(#реклама|#промо|#promo|#advertising|#ad\b)")
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "sports_news_logo.jpg"

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
        retry = await _request_rewrite(
            clean_text,
            source,
            examples,
            "\n\nВАЖЛИВА ПОВТОРНА ПРАВКА: попередній варіант повторив зміст хука у другому абзаці. Перепиши пост так, щоб другий абзац одразу давав нову деталь із джерела.",
        )
        retry["text"] = sanitize_source_text((retry.get("text") or "").strip())
        result = retry
    return result


def _open_image_bytes(image_bytes: bytes, label: str) -> Image.Image:
    if not image_bytes:
        raise RuntimeError(f"{label}_EMPTY")
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
        return image
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise RuntimeError(f"{label}_INVALID_IMAGE: {type(exc).__name__}: {exc}") from exc


def _normalize_to_jpeg(image_bytes: bytes, label: str) -> bytes:
    image = _open_image_bytes(image_bytes, label).convert("RGB")
    out = BytesIO()
    image.save(out, format="JPEG", quality=97, optimize=True)
    return out.getvalue()


def _to_four_three(image_bytes: bytes) -> bytes:
    image = _open_image_bytes(image_bytes, "GENERATED_IMAGE").convert("RGB")
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
    normalized = _normalize_to_jpeg(image_bytes, "SOURCE_IMAGE")
    return f"data:image/jpeg;base64,{base64.b64encode(normalized).decode('ascii')}"


def _load_logo() -> Image.Image:
    if not LOGO_PATH.exists():
        raise RuntimeError(f"SPORTS_NEWS_LOGO_MISSING: {LOGO_PATH}")
    try:
        logo = Image.open(LOGO_PATH)
        logo.load()
        return logo.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise RuntimeError(f"SPORTS_NEWS_LOGO_INVALID: {type(exc).__name__}: {exc}") from exc


def _add_sports_news_logo(image_bytes: bytes) -> bytes:
    image = _open_image_bytes(image_bytes, "WORKING_IMAGE").convert("RGBA")
    logo = _load_logo()
    width, height = image.size
    base = min(width, height)
    target_w = max(88, int(base * 0.145))
    scale = target_w / logo.width
    target_h = max(1, int(logo.height * scale))
    logo = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)
    margin_x = max(14, int(width * 0.022))
    margin_y = max(14, int(height * 0.022))
    image.alpha_composite(logo, (margin_x, margin_y))
    out = BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=96, optimize=True)
    return out.getvalue()


def _effective_image_model() -> str:
    configured = (settings.openai_image_model or "").strip()
    if not configured or configured == "gpt-image-1":
        return "gpt-image-2"
    return configured


async def _source_needs_cleanup(source_image: bytes) -> bool:
    try:
        response = await client.responses.create(
            model=settings.openai_model,
            input=[{
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Inspect this sports image and answer with exactly one word: CLEAN or EDIT. "
                            "Return EDIT if there is ANY added/overlaid graphic content that is not part of the natural photographed scene and should be removed before reposting. "
                            "That includes ALL overlaid headlines, captions, quotes, scores, dates, player names, lists, numbers, arrows, graphic labels, channel names, media logos, watermarks, bookmaker/casino/betting marks, sponsor blocks, promo badges and other added text or branding. "
                            "Return CLEAN only for a normal clean sports photograph with no added text, no added graphic labels, no watermarks and no foreign overlaid branding. "
                            "Do NOT treat natural physical details as overlays: keep club/team crests printed on the kit, jersey numbers, kit manufacturer marks, sponsors physically printed on clothing, tattoos, stadium signs and other details that genuinely exist in the photographed scene. "
                            "If you are uncertain whether something is an overlay, return EDIT."
                        ),
                    },
                    {"type": "input_image", "image_url": _image_data_url(source_image)},
                ],
            }],
        )
        verdict = response.output_text.strip().upper()
        log.info("Image cleanup classifier verdict=%s", verdict[:80])
        if verdict.startswith("EDIT"):
            return True
        if verdict.startswith("CLEAN"):
            return False
        raise RuntimeError(f"CLEANUP_CLASSIFIER_BAD_RESPONSE: {verdict[:200]}")
    except RuntimeError:
        raise
    except Exception as exc:
        log.exception("Cleanup classifier failed")
        raise RuntimeError(f"CLEANUP_CLASSIFIER_FAILED: {type(exc).__name__}: {exc}") from exc


async def _image_result_bytes(item) -> bytes | None:
    b64 = getattr(item, "b64_json", None)
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception as exc:
            raise RuntimeError(f"IMAGE_RESULT_B64_DECODE_FAILED: {type(exc).__name__}: {exc}") from exc
    url = getattr(item, "url", None)
    if url:
        try:
            async with httpx.AsyncClient(timeout=90) as http:
                response = await http.get(url)
                response.raise_for_status()
                return response.content
        except Exception as exc:
            raise RuntimeError(f"IMAGE_RESULT_DOWNLOAD_FAILED: {type(exc).__name__}: {exc}") from exc
    return None


def _restore_source_dimensions(edited_bytes: bytes, source_image: bytes) -> bytes:
    source = _open_image_bytes(source_image, "SOURCE_IMAGE")
    edited = _open_image_bytes(edited_bytes, "EDITED_IMAGE").convert("RGB")
    if edited.size != source.size:
        edited = edited.resize(source.size, Image.Resampling.LANCZOS)
    out = BytesIO()
    edited.save(out, format="JPEG", quality=96, optimize=True)
    return out.getvalue()


async def generate_news_image(news_text: str, *, source_image: bytes | None = None, template_key: str = DEFAULT_IMAGE_TEMPLATE) -> bytes:
    clean_context = re.sub(r"<[^>]+>", " ", news_text)[:1000]
    image_model = _effective_image_model()

    if source_image:
        source_image = _normalize_to_jpeg(source_image, "SOURCE_IMAGE")

        # Manual image editing is explicit user intent. Do not gate it behind the
        # vision classifier: the direct /testedit proved images.edit works, while
        # classifier false-CLEAN verdicts were skipping the edit entirely.
        log.info("Image edit path: forced manual edit model=%s", image_model)

        image_file = BytesIO(source_image)
        image_file.name = "source.jpg"
        try:
            result = await client.images.edit(
                model=image_model,
                image=image_file,
                prompt=(
                    "EDIT THIS EXACT SOURCE IMAGE; DO NOT CREATE A NEW SCENE. "
                    "Remove ALL added/overlaid text and graphic overlays from the image: headlines, captions, quote blocks, scores, dates, player names, lists, numbers, arrows, graphic labels, media/channel names, watermarks, bookmaker/casino/betting branding, sponsor/promo blocks and any other overlaid logos or promotional graphics. "
                    "If there are no removable overlays, preserve the source photograph as closely as possible and do not invent changes. "
                    "Reconstruct only the small areas hidden behind those overlays so they naturally match the surrounding original background. "
                    "Preserve the underlying sports photograph as faithfully as possible: same real person, face, expression, body, pose, clothing, crop, camera angle, stadium/background, colors, lighting, shadows and photographic texture. "
                    "Do not redesign, restyle, recolor, relight, beautify, sharpen, change anatomy or invent a different person. "
                    "Keep natural physical details that are genuinely part of the photographed scene, including club crests and jersey details printed on clothing, tattoos and real stadium elements. "
                    "Do not add any new text or branding. The final result must look like the clean original photograph before any poster text or graphic overlay was added. "
                    f"News context is for identification only and must NOT be rendered as text: {clean_context}"
                ),
            )
        except Exception as exc:
            log.exception("Image edit API failed model=%s", image_model)
            raise RuntimeError(f"IMAGE_EDIT_API_FAILED[{image_model}]: {type(exc).__name__}: {exc}") from exc

        if not result.data:
            raise RuntimeError(f"IMAGE_EDIT_EMPTY_RESULT[{image_model}]")

        edited_bytes = await _image_result_bytes(result.data[0])
        if not edited_bytes:
            raise RuntimeError(f"IMAGE_EDIT_RESULT_HAS_NO_IMAGE[{image_model}]")

        try:
            working = _restore_source_dimensions(edited_bytes, source_image)
        except Exception as exc:
            log.exception("Edited image validation failed")
            raise RuntimeError(f"IMAGE_EDIT_INVALID_RESULT: {type(exc).__name__}: {exc}") from exc

        return _add_sports_news_logo(working)

    selected_key, template = get_template(template_key, news_text)
    try:
        result = await client.images.generate(
            model=image_model,
            prompt=(
                "Create a clean photorealistic sports editorial photograph. "
                "No foreign media/channel/bookmaker/casino branding. No artificial text. "
                "Natural saturated colors, realistic skin, anatomy, clothing, lighting and shadows. "
                f"Composition reference: {selected_key}. {template['prompt']} "
                f"News context: {clean_context}"
            ),
            size="1536x1024",
        )
    except Exception as exc:
        log.exception("Image generation API failed model=%s", image_model)
        raise RuntimeError(f"IMAGE_GENERATE_API_FAILED[{image_model}]: {type(exc).__name__}: {exc}") from exc

    if result.data:
        generated = await _image_result_bytes(result.data[0])
        if generated:
            return _add_sports_news_logo(_to_four_three(generated))
    raise RuntimeError(f"IMAGE_API_EMPTY_RESULT[{image_model}]")