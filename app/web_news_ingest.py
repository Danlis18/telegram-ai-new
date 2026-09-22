import asyncio
import hashlib
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from urllib.parse import urljoin

import aiosqlite
import httpx

from app.config import settings
from app.tenant import user_scope

log = logging.getLogger("telegram-ai-news.web-news")

FEEDS = [
    ("ESPN Soccer", "https://www.espn.com/espn/rss/soccer/news", 1.00),
    ("ESPN Tennis", "https://www.espn.com/espn/rss/tennis/news", 0.94),
    ("ESPN Motorsport", "https://www.espn.com/espn/rss/rpm/news", 0.94),
    ("ESPN NBA", "https://www.espn.com/espn/rss/nba/news", 0.90),
    ("ESPN Boxing", "https://www.espn.com/espn/rss/boxing/news", 0.96),
    ("The Guardian Sport", "https://www.theguardian.com/sport/rss", 1.00),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml", 1.04),
    ("Sky News Sport", "https://feeds.skynews.com/feeds/rss/sports.xml", 0.92),
    ("Yahoo Sports", "https://sports.yahoo.com/rss/", 0.82),
    ("HLTV", "https://www.hltv.org/rss/news", 0.96),
]

STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "at", "with", "from", "as", "is", "are",
    "after", "before", "into", "over", "new", "latest", "live", "news", "says", "say", "amid", "more", "about",
    "this", "that", "their", "his", "her", "its", "vs", "v", "fc", "team", "club",
}
SPORT_HINTS = {
    "football", "soccer", "premier", "league", "champions", "arsenal", "liverpool", "chelsea", "barcelona", "madrid",
    "manchester", "bayern", "psg", "juventus", "inter", "milan", "dortmund", "shakhtar", "dynamo", "goal", "coach",
    "transfer", "tennis", "atp", "wta", "grand slam", "us open", "wimbledon", "alcaraz", "sinner", "djokovic",
    "boxing", "boxer", "fight", "heavyweight", "usyk", "fury", "canelo", "ufc", "mma",
    "formula 1", "f1", "verstappen", "norris", "ferrari", "mercedes", "mclaren",
    "nba", "basketball", "lakers", "celtics", "warriors", "knicks", "nuggets",
    "counter-strike", "counter strike", "cs2", "hltv", "navi", "vitality", "g2", "astralis", "faze",
}
HOT_HINTS = {
    "official", "confirmed", "signs", "signed", "transfer", "sacked", "fired", "resigns", "injury", "injured", "ban",
    "banned", "suspended", "record", "wins", "winner", "final", "semi-final", "derby", "shock", "upset", "eliminated",
    "qualifies", "qualified", "champion", "title", "breaking", "deal", "contract", "return", "out", "ruled out",
}
LOW_VALUE_HINTS = {"opinion", "newsletter", "podcast", "quiz", "watch", "live blog", "minute-by-minute", "gallery"}

_RUSSIA_HARD_BLOCK_RE = re.compile(
    r"(?iu)\\b(russia|russian|россия|российск\\w*|росі(?:я|ї|єю|ю)|російськ\\w*|рф\\b|"
    r"russian premier league|russia fnl|fnl 2|zenit|spartak(?: moscow)?|cska(?: moscow)?|"
    r"lokomotiv(?: moscow)?|dynamo moscow|rubin kazan|krasnodar|rostov|akhmat|sochi)\\b"
)

def _blocked_geo_text(value: str) -> bool:
    return bool(_RUSSIA_HARD_BLOCK_RE.search(value or ""))


@dataclass
class WebItem:
    source: str
    url: str
    title: str
    summary: str
    published: datetime
    authority: float
    image_url: str = ""


@dataclass
class Cluster:
    items: list[WebItem] = field(default_factory=list)

    @property
    def newest(self) -> WebItem:
        return max(self.items, key=lambda item: item.published)

    @property
    def sources(self) -> set[str]:
        return {item.source for item in self.items}

    @property
    def title(self) -> str:
        return self.newest.title


def _strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_date(value: str) -> datetime:
    value = (value or "").strip()
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _node_text(node, names: tuple[str, ...]) -> str:
    for child in list(node):
        tag = child.tag.split("}")[-1].lower()
        if tag in names:
            if tag == "link" and child.attrib.get("href"):
                return child.attrib["href"]
            return "".join(child.itertext()).strip()
    return ""


def _node_image(node, base_url: str) -> str:
    for child in node.iter():
        tag = child.tag.split("}")[-1].lower()
        url = str(child.attrib.get("url") or child.attrib.get("href") or "").strip()
        mime = str(child.attrib.get("type") or "").lower()
        medium = str(child.attrib.get("medium") or "").lower()
        if url and (tag in {"thumbnail", "image"} or medium == "image" or mime.startswith("image/")):
            return urljoin(base_url, url)
    raw = ET.tostring(node, encoding="unicode", method="html")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', raw, re.I)
    return urljoin(base_url, html.unescape(match.group(1))) if match else ""


def _parse_feed(content: bytes, source: str, authority: float) -> list[WebItem]:
    try:
        root = ET.fromstring(content)
    except Exception:
        return []
    nodes = [n for n in root.iter() if n.tag.split("}")[-1].lower() in {"item", "entry"}]
    result: list[WebItem] = []
    for node in nodes[:35]:
        title = _strip_html(_node_text(node, ("title",)))
        link = _node_text(node, ("link", "guid", "id"))
        summary = _strip_html(_node_text(node, ("description", "summary", "content", "encoded")))
        date_raw = _node_text(node, ("pubdate", "published", "updated", "date"))
        if not title or not link:
            continue
        result.append(WebItem(source, link.strip(), title[:500], summary[:1800], _parse_date(date_raw), authority, _node_image(node, link)))
    return result


async def _fetch_feed(client: httpx.AsyncClient, source: str, url: str, authority: float) -> list[WebItem]:
    try:
        response = await client.get(url, timeout=18)
        response.raise_for_status()
        return _parse_feed(response.content, source, authority)
    except Exception as exc:
        log.warning("Web sports feed unavailable source=%s: %s", source, exc)
        return []


def _tokens(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9а-яіїєґ]+", (value or "").casefold())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS}


def _similarity(a: str, b: str) -> float:
    aa, bb = _tokens(a), _tokens(b)
    return len(aa & bb) / max(1, min(len(aa), len(bb))) if aa and bb else 0.0


def _relevance_text(item: WebItem) -> str:
    return f"{item.title} {item.summary}".casefold()


def _sport_relevance(item: WebItem) -> int:
    return min(18, sum(1 for hint in SPORT_HINTS if hint in _relevance_text(item)) * 4)


def _hotness(item: WebItem) -> int:
    text = _relevance_text(item)
    return min(18, sum(1 for hint in HOT_HINTS if hint in text) * 4) - sum(1 for hint in LOW_VALUE_HINTS if hint in text) * 7


def _cluster_score(cluster: Cluster, now: datetime, max_age_h: float) -> float:
    newest = cluster.newest
    age_h = max(0.0, (now - newest.published).total_seconds() / 3600)
    freshness = max(0.0, 34.0 * (1.0 - age_h / max_age_h))
    corroboration = min(22.0, max(0, len(cluster.sources) - 1) * 9.0)
    authority = max(item.authority for item in cluster.items) * 15.0
    return freshness + corroboration + authority + _sport_relevance(newest) + _hotness(newest)


def _cluster_items(items: list[WebItem]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for item in sorted(items, key=lambda x: x.published, reverse=True):
        for cluster in clusters:
            if _similarity(item.title, cluster.title) >= 0.48:
                cluster.items.append(item)
                break
        else:
            clusters.append(Cluster([item]))
    return clusters


async def fetch_ranked_web_news(limit: int = 8) -> list[tuple[Cluster, float]]:
    max_age_h = float(os.getenv("WEB_NEWS_MAX_AGE_HOURS") or "12")
    now = datetime.now(timezone.utc)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AutoPostingSports/1.0)", "Accept": "application/rss+xml,application/xml,text/xml,text/html;q=0.8,*/*;q=0.5"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        groups = await asyncio.gather(*[_fetch_feed(client, *feed) for feed in FEEDS])
    items = [
        item for group in groups for item in group
        if -0.5 <= (now - item.published).total_seconds() / 3600 <= max_age_h
        and _sport_relevance(item) > 0
        and not _blocked_geo_text(f"{item.title} {item.summary} {item.url}")
    ]
    ranked = [(cluster, _cluster_score(cluster, now, max_age_h)) for cluster in _cluster_items(items)]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked[: max(1, int(limit))]


async def _near_duplicate(user_id: int, title: str) -> bool:
    title_tokens = _tokens(title)
    if not title_tokens:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT original_text,rewritten_text FROM news WHERE user_id=? AND created_at>=? ORDER BY id DESC LIMIT 180", (int(user_id), cutoff))
        rows = await cur.fetchall()
    for original, rewritten in rows:
        probe_tokens = _tokens(f"{original or ''} {rewritten or ''}")
        if probe_tokens and len(title_tokens & probe_tokens) / max(1, len(title_tokens)) >= 0.74:
            return True
    return False


def _source_key(cluster: Cluster) -> str:
    primary = re.sub(r"[^a-z0-9]+", "_", cluster.newest.source.casefold()).strip("_")[:30]
    return f"web:{primary or 'sports'}"


def _synthetic_message_id(cluster: Cluster) -> int:
    return int(hashlib.sha256((cluster.newest.url + cluster.title).encode()).hexdigest()[:12], 16) % 2_000_000_000


def _ai_input(cluster: Cluster, score: float) -> str:
    source_lines = []
    for item in sorted(cluster.items, key=lambda x: x.published, reverse=True)[:4]:
        source_lines.append(f"• {item.source}: {item.title}\n{item.summary[:650]}")
    return (
        "ІНТЕРНЕТ-НОВИНА. Перепиши тільки підтверджені факти. УВЕСЬ готовий пост має бути українською: "
        "переклади також прямі цитати, blockquote і будь-які англомовні речення; не залишай англійський текст, крім власних назв і загальновідомих назв турнірів/брендів. "
        "Готовий Telegram-пост тримай компактним, бажано до 650 символів, щоб він поміщався під креативом.\n\n"
        f"WebRank: {score:.0f}\nДжерела: {', '.join(sorted(cluster.sources))}\n\n" + "\n\n".join(source_lines)
    )


async def _daily_web_count(user_id: int) -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM news WHERE user_id=? AND source LIKE 'web:%' AND created_at>=datetime('now','start of day') AND status IN ('ready','scheduled','published')", (int(user_id),))
        return int((await cur.fetchone())[0])


async def _web_enabled(user_id: int) -> bool:
    from app.database import get_setting
    with user_scope(user_id):
        return (await get_setting("web_sources_enabled", "true") or "true").lower() == "true"


async def _web_limits(user_id: int) -> tuple[float, int]:
    from app.database import get_setting
    with user_scope(user_id):
        score = float(await get_setting("web_min_score", os.getenv("WEB_NEWS_MIN_SCORE") or "62") or 62)
        daily = int(await get_setting("web_max_per_day", os.getenv("WEB_NEWS_MAX_PER_DAY") or "8") or 8)
    return max(40.0, min(95.0, score)), max(1, min(20, daily))


async def _extract_article_image(item: WebItem) -> str:
    if item.image_url:
        return item.image_url
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=16) as client:
            r = await client.get(item.url)
            r.raise_for_status()
            text = r.text[:800_000]
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                return urljoin(item.url, html.unescape(m.group(1)))
    except Exception:
        log.debug("Could not extract article image url=%s", item.url, exc_info=True)
    return ""


async def _download_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=22) as client:
            r = await client.get(url)
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "image" not in ctype or len(r.content) < 10_000 or len(r.content) > 15_000_000:
                return None
            return r.content
    except Exception:
        return None


async def _make_creative(cluster: Cluster, rewritten: str) -> bytes:
    from app.ai_editor import generate_news_image
    source_bytes = None
    for item in sorted(cluster.items, key=lambda x: x.published, reverse=True):
        source_bytes = await _download_image(await _extract_article_image(item))
        if source_bytes:
            break
    if source_bytes:
        try:
            return await generate_news_image(rewritten, source_image=source_bytes)
        except Exception:
            log.exception("Could not clean source web image; generating original creative")
    return await generate_news_image(rewritten)


def _preview_caption(rewritten: str, news_id: int, sources: str, ai_score: int, web_score: float) -> str:
    from app.formatting import post_html
    body = post_html(rewritten)
    header = f"🌐 <b>Готовий web-пост #{news_id}</b>\nДжерела: <b>{html.escape(sources[:140])}</b>\n🧠 AI: <b>{ai_score}%</b> · WebRank: <b>{web_score:.0f}</b>\n\n"
    plain_len = len(re.sub(r"<[^>]+>", "", header + body))
    if plain_len <= 1000:
        return header + body
    plain = re.sub(r"<[^>]+>", "", rewritten).strip()
    return header + html.escape(plain[:700] + ("…" if len(plain) > 700 else ""))


async def process_web_news_once(user_id: int | None = None) -> dict:
    from app import main as app_main
    from app.ai_editor import is_advertising_post, rewrite_news
    from app.content_policy import can_accept_candidate, candidate_matches_preferences, cooldown_state
    from app.database import save, update_news
    from app.publishing import process_ready_automation

    user_id = int(user_id or settings.admin_user_id or 0)
    if not user_id or not await _web_enabled(user_id):
        return {"ok": True, "reason": "disabled"}
    threshold, max_daily = await _web_limits(user_id)
    if await _daily_web_count(user_id) >= max_daily:
        return {"ok": True, "reason": "daily_cap"}

    with user_scope(user_id):
        allowed, period, _, _ = await can_accept_candidate(user_id)
        if not allowed or not period:
            return {"ok": True, "reason": "quota"}
        if not (await cooldown_state(user_id, period)).get("allowed"):
            return {"ok": True, "reason": "cooldown"}

    ranked = await fetch_ranked_web_news(10)
    for cluster, web_score in ranked:
        cluster_blob = " ".join(f"{item.title} {item.summary} {item.url}" for item in cluster.items)
        if _blocked_geo_text(cluster_blob):
            log.info("Web candidate hard-blocked by Europe/Americas geo policy title=%s", cluster.title[:120])
            continue
        if web_score < threshold or await _near_duplicate(user_id, cluster.title):
            continue
        source = _source_key(cluster)
        raw = _ai_input(cluster, web_score)
        if is_advertising_post(raw)[0]:
            continue

        with user_scope(user_id):
            keep, _ = await candidate_matches_preferences(raw, source, user_id)
            if not keep:
                continue
            allowed, _, _, _ = await can_accept_candidate(user_id)
            if not allowed:
                break
            news_id = await save(source, _synthetic_message_id(cluster), raw, "", 0, "received")
            if not news_id:
                continue
            try:
                result = await rewrite_news(raw, source)
                rewritten = (result.get("text") or "").strip()
                ai_score = int(result.get("score") or 0)
                publishable = bool(result.get("publish")) and ai_score >= settings.min_publish_score and bool(rewritten)
                await update_news(news_id, rewritten_text=rewritten, score=ai_score, status="ready" if publishable else "rejected")
                if not publishable:
                    continue

                # HARD RULE: a web item never reaches the moderator without media.
                creative = await _make_creative(cluster, rewritten)
                buf = BytesIO(creative)
                buf.name = f"sports_news_web_{news_id}.jpg"
                sources = ", ".join(sorted(cluster.sources))
                sent = await app_main.publisher.send_photo(
                    int(user_id),
                    photo=buf,
                    caption=_preview_caption(rewritten, news_id, sources, ai_score, web_score),
                    parse_mode="HTML",
                    reply_markup=app_main.action_buttons(news_id, "photo"),
                )
                file_id = sent.photo[-1].file_id
                await update_news(news_id, media_type="photo", media_file_id=file_id, original_media_file_id=file_id)

                automation = await process_ready_automation(app_main.publisher, news_id, user_id)
                if automation.get("published"):
                    try:
                        await sent.edit_caption(
                            caption=f"✅ <b>Web-пост #{news_id} опубліковано автоматично</b>\n🧠 AI: <b>{ai_score}%</b> · WebRank: <b>{web_score:.0f}</b>",
                            parse_mode="HTML",
                            reply_markup=None,
                        )
                    except Exception:
                        pass
                return {"ok": True, "accepted": 1, "ranked": len(ranked), "media": True}
            except Exception as exc:
                await update_news(news_id, status="ai_error")
                log.exception("Web candidate processing/creative failed news_id=%s", news_id)
                # Never fall back to a bare text preview. It can retry next scan.
                continue
    return {"ok": True, "accepted": 0, "ranked": len(ranked)}


async def web_news_worker() -> None:
    from app.database import list_users
    initial = max(20, min(300, int(os.getenv("WEB_NEWS_INITIAL_DELAY_SECONDS") or "75")))
    interval = max(300, min(3600, int(os.getenv("WEB_NEWS_INTERVAL_SECONDS") or "900")))
    await asyncio.sleep(initial)
    while True:
        try:
            for row in await list_users(active_only=True):
                uid = int(row.get("telegram_user_id") or 0)
                if uid:
                    result = await process_web_news_once(uid)
                    log.info("Web sports discovery user=%s: %s", uid, result)
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Web sports discovery iteration failed")
        await asyncio.sleep(interval)


def start_web_news_worker() -> asyncio.Task:
    task = asyncio.create_task(web_news_worker(), name="web-sports-discovery")
    log.info("Multisource web sports discovery worker started")
    return task
