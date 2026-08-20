import html

SPORTS_NEWS_URL = "https://t.me/sports_news_ua"


def post_html(text: str) -> str:
    body = html.escape((text or "").strip())
    footer = f'<a href="{SPORTS_NEWS_URL}"><b>SPORTS NEWS</b></a> → на зв’язку.'
    return f"{body}\n\n{footer}" if body else footer


def post_plain(text: str) -> str:
    body = (text or "").strip()
    footer = "SPORTS NEWS → на зв’язку."
    return f"{body}\n\n{footer}" if body else footer
