import re

# These are COMPOSITION references only. Text/logo rendering is handled outside the
# image model so generated creatives never contain broken typography or duplicate branding.
IMAGE_TEMPLATES = {
    "transfer": {
        "label": "🔁 Transfer",
        "prompt": (
            "Transfer-news composition: one dominant athlete/coach cutout, cinematic club-color atmosphere, "
            "clean premium black/gold accents, strong depth and restrained diagonal geometry. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render any headline, caption, letters, numbers, badges, watermarks or logos."
        ),
    },
    "match_result": {
        "label": "🏟 Match result",
        "prompt": (
            "Match-result composition: two opposing athletes or an authentic celebration scene, stadium depth, "
            "energetic but uncluttered premium sports editorial framing. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render score text, team names, headlines, letters, numbers, watermarks or logos."
        ),
    },
    "goal_moment": {
        "label": "⚽ Goal / moment",
        "prompt": (
            "Goal or standout-moment composition: one athlete in an action-oriented crop, cinematic motion and depth, "
            "clean black/gold editorial accents without poster typography. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render any words, letters, numbers, watermarks or logos."
        ),
    },
    "quote": {
        "label": "💬 Quote",
        "prompt": (
            "Editorial portrait composition: realistic coach/player portrait, subtle stadium or press atmosphere, "
            "clean negative space and premium sports-news lighting. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render quotes, quotation text, names, letters, numbers, watermarks or logos."
        ),
    },
    "rivalry": {
        "label": "🔥 Rivalry",
        "prompt": (
            "Big-match rivalry composition: two opposing athletes facing into the frame, dramatic stadium lighting, "
            "balanced center space and restrained gold energy accents. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render matchup text, scores, letters, numbers, watermarks or logos."
        ),
    },
    "rumour": {
        "label": "👀 Rumour",
        "prompt": (
            "Premium sports-news portrait composition: one dominant athlete/coach, layered editorial depth, "
            "restrained intrigue, clean black/gold accents and realistic photographic texture. "
            "Keep the upper-left branding safe-zone visually calm and free of faces/hands/key objects. "
            "Do not render any headline, caption, letters, numbers, watermarks or logos."
        ),
    },
}

DEFAULT_IMAGE_TEMPLATE = "auto"


def choose_template(news_text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", news_text).lower()
    if any(x in t for x in ("сказав", "заявив", "повідомив", "прокоментував", "цитат", "вважає", "розповів")):
        return "quote"
    if any(x in t for x in ("перех", "трансфер", "оренд", "підписав", "контракт", "викуп")):
        return "transfer"
    if any(x in t for x in ("може змінити", "цікавиться", "інтерес", "переговор", "сфері інтерес", "майбутн")):
        return "rumour"
    if any(x in t for x in ("ель-класіко", "дербі", "протистоян", "vs ", "суперник")):
        return "rivalry"
    if re.search(r"\b\d+\s*[:–-]\s*\d+\b", t) or any(x in t for x in ("переміг", "обіграв", "поразк", "нічия", "рахун")):
        return "match_result"
    if any(x in t for x in ("гол", "забив", "дубль", "хет-трик", "асист", "м'яч")):
        return "goal_moment"
    return "rumour"


def get_template(key: str, news_text: str = ""):
    selected = choose_template(news_text) if key == "auto" else key
    return selected, IMAGE_TEMPLATES.get(selected, IMAGE_TEMPLATES["rumour"])
