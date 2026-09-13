import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import httpx

from app.config import settings
from app.formatting import post_html
from app.premium_emoji_registry import resolve_emoji

log = logging.getLogger("telegram-ai-news.match-schedule")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"

LEAGUES = [
    ("uefa.champions", "Ліга чемпіонів", "🏆", 42),
    ("eng.1", "Прем'єр-ліга", "🏴", 36),
    ("esp.1", "Ла Ліга", "🇪🇸", 33),
    ("ita.1", "Серія A", "🇮🇹", 30),
    ("ger.1", "Бундесліга", "🇩🇪", 30),
    ("fra.1", "Ліга 1", "🇫🇷", 26),
    ("uefa.europa", "Ліга Європи", "🟠", 28),
    ("uefa.europa.conf", "Ліга конференцій", "🟢", 20),
    ("eng.fa", "Кубок Англії", "🏆", 25),
    ("esp.copa_del_rey", "Кубок Іспанії", "🏆", 23),
    ("ger.dfb_pokal", "Кубок Німеччини", "🏆", 22),
    ("ita.coppa_italia", "Кубок Італії", "🏆", 22),
]

TEAM_WEIGHT = {
    "real madrid": 18, "barcelona": 18, "manchester city": 18, "manchester united": 17,
    "liverpool": 18, "arsenal": 17, "chelsea": 16, "tottenham hotspur": 14,
    "bayern munich": 18, "borussia dortmund": 14, "paris saint-germain": 17,
    "inter milan": 16, "internazionale": 16, "ac milan": 15, "juventus": 15,
    "atletico madrid": 15, "napoli": 14, "roma": 12, "newcastle united": 13,
    "benfica": 13, "porto": 12, "sporting cp": 12, "ajax": 11, "psv": 10,
    "shakhtar donetsk": 14, "dynamo kyiv": 13, "dynamo kiev": 13,
    "bayer leverkusen": 13, "rb leipzig": 11, "marseille": 11, "monaco": 10,
    "athletic club": 10, "real betis": 10, "sevilla": 10, "villarreal": 10,
}

TEAM_UK = {
    "Real Madrid": "Реал Мадрид", "Barcelona": "Барселона", "Manchester City": "Манчестер Сіті",
    "Manchester United": "Манчестер Юнайтед", "Liverpool": "Ліверпуль", "Arsenal": "Арсенал",
    "Chelsea": "Челсі", "Tottenham Hotspur": "Тоттенгем", "Bayern Munich": "Баварія",
    "Borussia Dortmund": "Боруссія Дортмунд", "Paris Saint-Germain": "ПСЖ", "PSG": "ПСЖ",
    "Inter Milan": "Інтер", "Internazionale": "Інтер", "AC Milan": "Мілан", "Juventus": "Ювентус",
    "Atletico Madrid": "Атлетіко", "Atlético Madrid": "Атлетіко", "Napoli": "Наполі", "Roma": "Рома",
    "Newcastle United": "Ньюкасл", "Benfica": "Бенфіка", "Porto": "Порту", "Sporting CP": "Спортінг",
    "Ajax": "Аякс", "PSV": "ПСВ", "Shakhtar Donetsk": "Шахтар", "Dynamo Kyiv": "Динамо Київ",
    "Dynamo Kiev": "Динамо Київ", "Bayer Leverkusen": "Баєр", "RB Leipzig": "РБ Лейпциг",
    "Marseille": "Марсель", "Monaco": "Монако", "Athletic Club": "Атлетік", "Real Betis": "Бетіс",
    "Sevilla": "Севілья", "Villarreal": "Вільярреал",
}


@dataclass
class Fixture:
    event_id: str
    league_code: str
    league_name: str
    league_emoji: str
    league_weight: int
    kickoff: datetime
    home: str
    away: str
    completed: bool = False

    @property
    def score(self) -> int:
        home_w = TEAM_WEIGHT.get(self.home.casefold(), 0)
        away_w = TEAM_WEIGHT.get(self.away.casefold(), 0)
        star = home_w + away_w
        if home_w >= 12 and away_w >= 12:
            star += 12
        elif home_w >= 10 and away_w >= 10:
            star += 7
        return self.league_weight + star


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.publish_timezone or "Europe/Kyiv")
    except Exception:
        return ZoneInfo("Europe/Kyiv")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _team_name(comp: dict) -> str:
    team = comp.get("team") or {}
    return str(team.get("displayName") or team.get("shortDisplayName") or team.get("name") or "").strip()


async def _fetch_league(client: httpx.AsyncClient, code: str, name: str, emoji: str, weight: int, date_key: str) -> list[Fixture]:
    try:
        response = await client.get(
            ESPN_SCOREBOARD.format(league=code),
            params={"dates": date_key, "limit": "100"},
            timeout=16,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("Fixture source failed league=%s: %s", code, exc)
        return []

    fixtures: list[Fixture] = []
    for event in payload.get("events") or []:
        competition = ((event.get("competitions") or [{}])[0]) or {}
        competitors = competition.get("competitors") or []
        home_comp = next((x for x in competitors if x.get("homeAway") == "home"), competitors[0] if competitors else {})
        away_comp = next((x for x in competitors if x.get("homeAway") == "away"), competitors[1] if len(competitors) > 1 else {})
        home, away = _team_name(home_comp), _team_name(away_comp)
        kickoff = _parse_iso(str(event.get("date") or competition.get("date") or ""))
        status = ((competition.get("status") or event.get("status") or {}).get("type") or {})
        completed = bool(status.get("completed"))
        if not home or not away or not kickoff:
            continue
        fixtures.append(Fixture(
            event_id=str(event.get("id") or f"{code}:{home}:{away}:{kickoff.isoformat()}"),
            league_code=code,
            league_name=name,
            league_emoji=emoji,
            league_weight=weight,
            kickoff=kickoff,
            home=home,
            away=away,
            completed=completed,
        ))
    return fixtures


async def fetch_top_fixtures(limit: int = 5, now: datetime | None = None) -> list[Fixture]:
    local_now = now.astimezone(_tz()) if now else datetime.now(_tz())
    date_key = local_now.strftime("%Y%m%d")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AutoPostingSports/1.0)",
        "Accept": "application/json,text/plain,*/*",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        groups = await asyncio.gather(*[
            _fetch_league(client, code, name, emoji, weight, date_key)
            for code, name, emoji, weight in LEAGUES
        ])

    by_id: dict[str, Fixture] = {}
    for group in groups:
        for fixture in group:
            kickoff_local = fixture.kickoff.astimezone(_tz())
            if kickoff_local.date() != local_now.date() or fixture.completed:
                continue
            if kickoff_local < local_now - timedelta(minutes=35):
                continue
            previous = by_id.get(fixture.event_id)
            if previous is None or fixture.score > previous.score:
                by_id[fixture.event_id] = fixture

    ranked = sorted(by_id.values(), key=lambda f: (-f.score, f.kickoff))
    strong = [item for item in ranked if item.score >= 34]
    return (strong or ranked)[: max(1, min(5, int(limit)))]


def _uk_team(name: str) -> str:
    return TEAM_UK.get(name, name)


async def render_fixtures_post(fixtures: list[Fixture], now: datetime | None = None) -> str:
    local_now = now.astimezone(_tz()) if now else datetime.now(_tz())
    month_names = {
        1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
        7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
    }
    lines = [
        "📅 <b>ТОП-МАТЧІ НА СЬОГОДНІ</b>",
        f"{local_now.day} {month_names[local_now.month]} • обрали найцікавіші події дня",
        "",
    ]
    for fixture in fixtures:
        home_uk, away_uk = _uk_team(fixture.home), _uk_team(fixture.away)
        league_icon = await resolve_emoji([fixture.league_name, fixture.league_code], fixture.league_emoji)
        home_icon = await resolve_emoji([home_uk, fixture.home], "⚽")
        away_icon = await resolve_emoji([away_uk, fixture.away], "⚽")
        kickoff = fixture.kickoff.astimezone(_tz()).strftime("%H:%M")
        lines.append(f"{league_icon} <b>{fixture.league_name}</b>")
        lines.append(f"⏰ <b>{kickoff}</b>  {home_icon} <b>{home_uk}</b> — {away_icon} <b>{away_uk}</b>")
        lines.append("")
    lines.append("Зберігай розклад, щоб не пропустити головне 👀")
    return "\n".join(lines).strip()


async def ensure_match_schedule_schema() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS daily_match_posts (
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                fixture_ids TEXT,
                post_text TEXT,
                published_at DATETIME,
                error TEXT,
                PRIMARY KEY(day,user_id)
            )"""
        )
        await db.commit()


async def _already_published(day: str, user_id: int) -> bool:
    await ensure_match_schedule_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT status FROM daily_match_posts WHERE day=? AND user_id=?", (day, int(user_id)))
        row = await cur.fetchone()
        return bool(row and row[0] == "published")


async def _record(day: str, user_id: int, status: str, fixtures: list[Fixture], text: str = "", error: str = "") -> None:
    await ensure_match_schedule_schema()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO daily_match_posts(day,user_id,status,fixture_ids,post_text,published_at,error)
               VALUES (?,?,?,?,?,CASE WHEN ?='published' THEN CURRENT_TIMESTAMP ELSE NULL END,?)
               ON CONFLICT(day,user_id) DO UPDATE SET
                 status=excluded.status,fixture_ids=excluded.fixture_ids,post_text=excluded.post_text,
                 published_at=CASE WHEN excluded.status='published' THEN CURRENT_TIMESTAMP ELSE daily_match_posts.published_at END,
                 error=excluded.error""",
            (day, int(user_id), status, json.dumps([f.event_id for f in fixtures]), text, status, error[:1000]),
        )
        await db.commit()


async def _publish_html_to_default_channel(user_id: int, body_html: str) -> None:
    from app.database import get_default_target
    from app import user_publisher

    target = await get_default_target(int(user_id))
    if not target:
        raise RuntimeError("No default publication channel configured")
    client = await user_publisher._ready_client()
    if client is None:
        raise RuntimeError("Premium publisher is not configured")
    entity = await user_publisher._resolve_target(client, target["channel_ref"])
    message, entities = user_publisher.telegram_html_to_mtproto(post_html(body_html))
    await client.send_message(entity, message, formatting_entities=entities, link_preview=False)


def _trigger_time() -> tuple[int, int]:
    raw = (os.getenv("DAILY_FIXTURES_TIME") or "09:00").strip()
    try:
        h, m = raw.split(":", 1)
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 9, 0


async def publish_daily_fixtures_once(user_id: int | None = None) -> dict:
    user_id = int(user_id or settings.admin_user_id or 0)
    if not user_id:
        return {"ok": False, "reason": "owner_not_configured"}
    local_now = datetime.now(_tz())
    day = local_now.date().isoformat()
    if await _already_published(day, user_id):
        return {"ok": True, "already": True}

    fixtures = await fetch_top_fixtures(5, local_now)
    if not fixtures:
        await _record(day, user_id, "empty", [], error="No qualifying fixtures found")
        return {"ok": False, "reason": "no_fixtures"}
    text = await render_fixtures_post(fixtures, local_now)
    try:
        await _publish_html_to_default_channel(user_id, text)
        await _record(day, user_id, "published", fixtures, text=text)
        log.info("Published daily top-match schedule user=%s day=%s fixtures=%d", user_id, day, len(fixtures))
        return {"ok": True, "published": True, "count": len(fixtures)}
    except Exception as exc:
        await _record(day, user_id, "error", fixtures, text=text, error=f"{type(exc).__name__}: {exc}")
        log.exception("Daily match schedule publish failed user=%s day=%s", user_id, day)
        return {"ok": False, "reason": str(exc)}


async def match_schedule_worker() -> None:
    await ensure_match_schedule_schema()
    # Give Telegram backfill a moment to teach the semantic Premium emoji registry
    # before today's schedule is rendered for the first time after a deployment.
    await asyncio.sleep(max(30, min(300, int(os.getenv("DAILY_FIXTURES_INITIAL_DELAY_SECONDS") or "150"))))
    while True:
        try:
            local_now = datetime.now(_tz())
            trigger_h, trigger_m = _trigger_time()
            if (local_now.hour, local_now.minute) >= (trigger_h, trigger_m):
                await publish_daily_fixtures_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daily fixture worker iteration failed")
        await asyncio.sleep(300)


def start_match_schedule_worker() -> asyncio.Task:
    task = asyncio.create_task(match_schedule_worker(), name="daily-top-matches")
    log.info("Daily top-match worker started time=%02d:%02d %s", *_trigger_time(), _tz().key)
    return task
