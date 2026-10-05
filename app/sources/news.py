"""AI/ML news from RSS feeds, with a recency window and a cheap anti-clickbait filter."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from ..config import Settings
from ..models import NewsItem

log = logging.getLogger(__name__)

CLICKBAIT = re.compile(
    r"you won'?t believe|shocking|mind[- ]?blowing|game[- ]?changer|insane|destroy|"
    r"\bвы не поверите\b|шок|сенсаци|невероятн|взорвал|топ-?\d+|\d+ способов|лайфхак|"
    r"sponsored|webinar|вебинар|промокод|скидк",
    re.IGNORECASE,
)


def is_clickbait(title: str) -> bool:
    return bool(CLICKBAIT.search(title)) or title.count("!") >= 2


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _text(html: str, limit: int = 400) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return text[:limit]


def parse_feed(raw: bytes, feed_url: str, since: datetime) -> list[NewsItem]:
    parsed = feedparser.parse(raw)
    source = parsed.feed.get("title") or urlparse(feed_url).netloc
    items = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = entry.get("link") or ""
        if not title or not link or is_clickbait(title):
            continue
        published = _published(entry)
        if published and published < since:
            continue
        uid = entry.get("id") or link
        items.append(
            NewsItem(
                id=hashlib.sha1(uid.encode()).hexdigest()[:16],
                title=title,
                url=link,
                source=source,
                published=published,
                summary=_text(entry.get("summary", "")),
            )
        )
    return items


async def _fetch_one(session: aiohttp.ClientSession, url: str, since: datetime) -> list[NewsItem]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            raw = await r.read()
        return parse_feed(raw, url, since)
    except Exception as exc:  # one broken feed must not break the digest
        log.warning("Feed %s failed: %s", url, exc)
        return []


async def fetch_news(settings: Settings, session: aiohttp.ClientSession) -> list[NewsItem]:
    since = datetime.now(timezone.utc) - timedelta(hours=settings.news_lookback_hours)
    results = await asyncio.gather(*(_fetch_one(session, u, since) for u in settings.feeds))
    # Round-robin across feeds so one prolific source doesn't dominate
    merged: list[NewsItem] = []
    for i in range(max((len(r) for r in results), default=0)):
        merged.extend(r[i] for r in results if i < len(r))
    log.info("News: %d items from %d feeds", len(merged), len(settings.feeds))
    return merged
