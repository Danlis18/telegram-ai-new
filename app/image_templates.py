import re

IMAGE_TEMPLATES = {
    "transfer": {
        "label": "🔁 Transfer",
        "prompt": "Transfer-news card: one dominant player cutout, cinematic club-color atmosphere, strong directional diagonal/arrow geometry, short headline zone, premium black/gold SPORTS NEWS identity.",
    },
    "match_result": {
        "label": "🏟 Match result",
        "prompt": "Match-result card: two opposing players or a celebration scene, stadium depth, compact score/result zone, energetic composition, premium black/gold SPORTS NEWS identity.",
    },
    "goal_moment": {
        "label": "⚽ Goal / moment",
        "prompt": "Goal or standout-moment card: one player in an action-oriented crop, dynamic diagonal accents, strong depth and motion, compact headline zone, premium black/gold SPORTS NEWS identity.",
    },
    "quote": {
        "label": "💬 Quote",
        "prompt": "Quote card: realistic coach/player portrait on one side, large clean quote area on the other, elegant quotation-mark motif, restrained cinematic background, premium black/gold SPORTS NEWS identity.",
    },
    "rivalry": {
        "label": "🔥 Rivalry",
        "prompt": "Big-match/rivalry card: two opposing athletes facing into the composition, dramatic stadium light, central matchup/result area, subtle gold energy accents, premium SPORTS NEWS identity.",
    },
    "rumour": {
        "label": "👀 Rumour",
        "prompt": "Rumour/interest card: one dominant player portrait, layered editorial diagonals and depth, restrained intrigue rather than clickbait, compact headline/subheadline zones, premium black/gold SPORTS NEWS identity.",
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
