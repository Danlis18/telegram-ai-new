import asyncio
import hashlib
import html
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

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


@dataclass
class WebItem:
    source: str
    url: str
    title: str
    summary: str
    published: datetime
    authority: float


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

    @property
    def summary(self) -> str:
        return max((item.summary for item in self.items), key=len, default="")


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
        result.append(WebItem(source, link.strip(), title[:500], summary[:1800], _parse_date(date_raw), authority))
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
    if not aa or not bb:
        return 0.0
    inter = len(aa & bb)
    return inter / max(1, min(len(aa), len(bb)))


def _relevance_text(item: WebItem) -> str:
    return f"{item.title} {item.summary}".casefold()


def _sport_relevance(item: WebItem) -> int:
    text = _relevance_text(item)
    hits = sum(1 for hint in SPORT_HINTS if hint in text)
    return min(18, hits * 4)


def _hotness(item: WebItem) -> int:
    text = _relevance_text(item)
    hits = sum(1 for hint in HOT_HINTS if hint in text)
    low = sum(1 for hint in LOW_VALUE_HINTS if hint in text)
    return min(18, hits * 4) - low * 7


def _cluster_score(cluster: Cluster, now: datetime, max_age_h: float) -> float:
    newest = cluster.newest
    age_h = max(0.0, (now - newest.published).total_seconds() / 3600)
    freshness = max(0.0, 34.0 * (1.0 - age_h / max_age_h))
    corroboration = min(22.0, max(0, len(cluster.sources) - 1) * 9.0)
    authority = max(item.authority for item in cluster.items) * 15.0
    relevance = _sport_relevance(newest)
    hotness = _hotness(newest)
    return freshness + corroboration + authority + relevance + hotness


def _cluster_items(items: list[WebItem]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for item in sorted(items, key=lambda x: x.published, reverse=True):
        placed = False
        for cluster in clusters:
            if _similarity(item.title, cluster.title) >= 0.48:
                cluster.items.append(item)
                placed = True
                break
        if not placed:
            clusters.append(Cluster([item]))
    return clusters


async def fetch_ranked_web_news(limit: int = 8) -> list[tuple[Cluster, float]]:
    max_age_h = float(os.getenv("WEB_NEWS_MAX_AGE_HOURS") or "12")
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AutoPostingSports/1.0; +https://t.me/sports_news_ua)",
        "Accept": "application/rss+xml,application/xml,text/xml,text/html;q=0.8,*/*;q=0.5",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        groups = await asyncio.gather(*[_fetch_feed(client, *feed) for feed in FEEDS])
    items = []
    for group in groups:
        for item in group:
            age_h = (now - item.published).total_seconds() / 3600
            if -0.5 <= age_h <= max_age_h and _sport_relevance(item) > 0:
                items.append(item)
    ranked = [(cluster, _cluster_score(cluster, now, max_age_h)) for cluster in _cluster_items(items)]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked[: max(1, int(limit))]


async def _near_duplicate(user_id: int, title: str) -> bool:
    title_tokens = _tokens(title)
    if not title_tokens:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT original_text,rewritten_text FROM news WHERE user_id=? AND created_at>=? ORDER BY id DESC LIMIT 180",
            (int(user_id), cutoff),
        )
        rows = await cur.fetchall()
    for original, rewritten in rows:
        probe = f"{original or ''} {rewritten or ''}"
        probe_tokens = _tokens(probe)
        if not probe_tokens:
            continue
        overlap = len(title_tokens & probe_tokens) / max(1, len(title_tokens))
        if overlap >= 0.74:
            return True
    return False


def _source_key(cluster: Cluster) -> str:
    primary = cluster.newest.source.casefold()
    primary = re.sub(r"[^a-z0-9]+", "_", primary).strip("_")[:30]
    return f"web:{primary or 'sports'}"


def _synthetic_message_id(cluster: Cluster) -> int:
    digest = hashlib.sha256((cluster.newest.url + cluster.title).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % 2_000_000_000


def _ai_input(cluster: Cluster, score: float) -> str:
    sources = ", ".join(sorted(cluster.sources))
    source_lines = []
    for item in sorted(cluster.items, key=lambda x: x.published, reverse=True)[:4]:
        source_lines.append(f"• {item.source}: {item.title}\n{item.summary[:650]}")
    return (
        "ІНТЕРНЕТ-НОВИНА. Нижче зібрані свіжі сигнали з незалежних спортивних джерел. "
        "Перепиши тільки факти, які реально випливають із матеріалів. Не вигадуй деталей.\n\n"
        f"Внутрішній рейтинг актуальності/цікавості: {score:.0f}/100+\n"
        f"Джерела: {sources}\n\n" + "\n\n".join(source_lines)
    )


async def _daily_web_count(user_id: int) -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """SELECT COUNT(*) FROM news WHERE user_id=? AND source LIKE 'web:%'
               AND created_at>=datetime('now','start of day') AND status IN ('ready','scheduled','published')""",
            (int(user_id),),
        )
        return int((await cur.fetchone())[0])


async def process_web_news_once(user_id: int | None = None) -> dict:
    from app import main as app_main
    from app.ai_editor import is_advertising_post, rewrite_news
    from app.content_policy import can_accept_candidate, candidate_matches_preferences, cooldown_state
    from app.database import get_news, save, update_news
    from app.formatting import post_html
    from app.publishing import process_ready_automation

    user_id = int(user_id or settings.admin_user_id or 0)
    if not user_id:
        return {"ok": False, "reason": "owner_not_configured"}
    max_daily = max(1, min(20, int(os.getenv("WEB_NEWS_MAX_PER_DAY") or "8")))
    if await _daily_web_count(user_id) >= max_daily:
        return {"ok": True, "reason": "daily_cap"}

    with user_scope(user_id):
        allowed, period, _, _ = await can_accept_candidate(user_id)
        if not allowed or not period:
            return {"ok": True, "reason": "quota"}
        cooldown = await cooldown_state(user_id, period)
        if not cooldown.get("allowed"):
            return {"ok": True, "reason": "cooldown"}

    ranked = await fetch_ranked_web_news(10)
    threshold = float(os.getenv("WEB_NEWS_MIN_SCORE") or "62")
    accepted = 0
    for cluster, web_score in ranked:
        if web_score < threshold:
            continue
        if await _near_duplicate(user_id, cluster.title):
            continue

        source = _source_key(cluster)
        raw = _ai_input(cluster, web_score)
        advertising, _ = is_advertising_post(raw)
        if advertising:
            continue

        with user_scope(user_id):
            keep, preference_reason = await candidate_matches_preferences(raw, source, user_id)
            if not keep:
                log.info("Web candidate rejected by learned preference source=%s: %s", source, preference_reason)
                continue
            allowed, period, _, _ = await can_accept_candidate(user_id)
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

                automation = await process_ready_automation(app_main.publisher, news_id, user_id)
                if automation.get("published"):
                    await app_main.notify_user(
                        user_id,
                        f"🌐 <b>Web-пост #{news_id} опубліковано автоматично</b>\n"
                        f"🧠 AI: <b>{ai_score}%</b> · WebRank: <b>{web_score:.0f}</b>",
                    )
                else:
                    await app_main.notify_user(
                        user_id,
                        f"🌐 <b>Готовий web-пост #{news_id}</b>\n"
                        f"Джерела: <b>{html.escape(', '.join(sorted(cluster.sources))[:160])}</b>\n"
                        f"🧠 AI: <b>{ai_score}%</b> · WebRank: <b>{web_score:.0f}</b>\n\n"
                        f"{post_html(rewritten)}",
                        app_main.action_buttons(news_id, None),
                    )
                accepted += 1
            except Exception:
                await update_news(news_id, status="ai_error")
                log.exception("Web candidate AI processing failed news_id=%s", news_id)

        # One high-quality web item per scan is deliberate; normal period quotas and
        # cooldown then keep Telegram + web content balanced rather than flooding.
        if accepted >= 1:
            break
    return {"ok": True, "accepted": accepted, "ranked": len(ranked)}


async def web_news_worker() -> None:
    initial = max(20, min(300, int(os.getenv("WEB_NEWS_INITIAL_DELAY_SECONDS") or "75")))
    interval = max(300, min(3600, int(os.getenv("WEB_NEWS_INTERVAL_SECONDS") or "900")))
    await asyncio.sleep(initial)
    while True:
        try:
            result = await process_web_news_once()
            log.info("Web sports discovery iteration: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Web sports discovery iteration failed")
        await asyncio.sleep(interval)


def start_web_news_worker() -> asyncio.Task:
    task = asyncio.create_task(web_news_worker(), name="web-sports-discovery")
    log.info("Multisource web sports discovery worker started")
    return task
