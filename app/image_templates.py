import random

IMAGE_TEMPLATES = {
    "spotlight": {
        "label": "🎯 Spotlight",
        "prompt": (
            "Minimal premium sports editorial poster. One main athlete/coach as the dominant subject, "
            "dark cinematic background with one restrained accent glow, subtle geometric depth, "
            "clean negative space, high-end football media look, no clutter."
        ),
    },
    "transfer": {
        "label": "🔁 Transfer",
        "prompt": (
            "Premium transfer-news visual. Main player portrait/cutout, abstract directional movement, "
            "two-tone club-inspired atmosphere without copying club logos, dark background, "
            "clean layered paper/card geometry, elegant and minimal, no sponsor branding."
        ),
    },
    "matchday": {
        "label": "⚽ Matchday",
        "prompt": (
            "Modern matchday sports graphic. Strong central athlete or two-sided composition, "
            "stadium/field atmosphere abstracted into a dark premium backdrop, subtle score-card geometry "
            "but no generated text or numbers, disciplined layout, energetic but minimal."
        ),
    },
    "stats": {
        "label": "📊 Stats",
        "prompt": (
            "Editorial sports statistics card style. Main athlete on one side, large clean empty regions "
            "reserved for possible stats overlays, subtle grid/data motifs, dark background with one bright accent, "
            "premium broadcast aesthetic, minimal, no generated text or fake numbers."
        ),
    },
    "breaking": {
        "label": "⚡ Breaking",
        "prompt": (
            "High-impact breaking sports news poster. Strong close portrait, dramatic dark red/orange or amber light, "
            "bold rectangular editorial bands and clean hierarchy, urgent but tasteful, premium European sports-media look, "
            "minimal decoration, no generated text."
        ),
    },
}

DEFAULT_IMAGE_TEMPLATE = "auto"


def get_template(key: str):
    if key == "auto":
        return random.choice(list(IMAGE_TEMPLATES.values()))
    return IMAGE_TEMPLATES.get(key, IMAGE_TEMPLATES["spotlight"])
