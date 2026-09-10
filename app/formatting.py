import html
import re

SPORTS_NEWS_URL = "https://t.me/sports_news_ua"
PREMIUM_ROCKET_ID = "5411580731929411768"
PREMIUM_ROCKET_HTML = f'<tg-emoji emoji-id="{PREMIUM_ROCKET_ID}">🚀</tg-emoji>'

# Keep only the Telegram HTML we explicitly support. Custom emoji are accepted
# only with a numeric emoji-id so arbitrary attributes/HTML cannot slip through.
_ALLOWED_TAG_RE = re.compile(
    r'</?(?:b|i|blockquote)>'
    r'|<tg-emoji\s+emoji-id=(?:"\d+"|\'\d+\')\s*>'
    r'|</tg-emoji>',
    re.IGNORECASE,
)


def _safe_telegram_html(text: str) -> str:
    """Escape arbitrary HTML while preserving supported Telegram formatting."""
    raw = (text or "").strip()
    if not raw:
        return ""

    placeholders: dict[str, str] = {}

    def keep_tag(match: re.Match) -> str:
        key = f"__TG_TAG_{len(placeholders)}__"
        tag = match.group(0).lower()
        placeholders[key] = tag
        return key

    protected = _ALLOWED_TAG_RE.sub(keep_tag, raw)
    escaped = html.escape(protected)
    for key, tag in placeholders.items():
        escaped = escaped.replace(key, tag)
    return escaped


def post_html(text: str) -> str:
    body = _safe_telegram_html(text)
    footer = f'<a href="{SPORTS_NEWS_URL}"><b>SPORTS NEWS</b></a> → на зв’язку {PREMIUM_ROCKET_HTML}'
    return f"{body}\n\n{footer}" if body else footer


def post_plain(text: str) -> str:
    body = _ALLOWED_TAG_RE.sub("", (text or "").strip())
    footer = "SPORTS NEWS → на зв’язку 🚀"
    return f"{body}\n\n{footer}" if body else footer
