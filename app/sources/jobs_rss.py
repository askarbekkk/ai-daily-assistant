"""Remote-job RSS feeds (no auth needed), filtered with the same title regexes as HH.ru."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser

from ..config import Settings
from ..models import Vacancy
from .hh import filter_vacancies
from .news import _published, _text

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-daily-assistant/1.0)"}


def parse_job_feed(raw: bytes, since: datetime) -> list[Vacancy]:
    out = []
    for entry in feedparser.parse(raw).entries:
        title, link = (entry.get("title") or "").strip(), entry.get("link") or ""
        if not title or not link:
            continue
        published = _published(entry)
        if published and published < since:
            continue
        company = ""
        if ": " in title:  # We Work Remotely style "Company: Title"
            company, title = title.split(": ", 1)
        company = company or entry.get("author", "") or ""
        out.append(
            Vacancy(
                id="rss:" + hashlib.sha1((entry.get("id") or link).encode()).hexdigest()[:16],
                title=title,
                company=company.strip(),
                url=link,
                schedule="remote",
                snippet=_text(entry.get("summary", ""), 300),
            )
        )
    return out


async def _fetch_one(session: aiohttp.ClientSession, url: str, since: datetime) -> list[Vacancy]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            return parse_job_feed(await r.read(), since)
    except Exception as exc:
        log.warning("Job feed %s failed: %s", url, exc)
        return []


async def fetch_rss_jobs(settings: Settings, session: aiohttp.ClientSession) -> list[Vacancy]:
    since = datetime.now(timezone.utc) - timedelta(days=settings.job_feeds_lookback_days)
    results = await asyncio.gather(*(_fetch_one(session, u, since) for u in settings.job_feed_urls))
    items = [v for r in results for v in r]
    filtered = filter_vacancies(items, settings.job_include, settings.job_exclude)
    log.info("Job RSS: %d fetched, %d after filter", len(items), len(filtered))
    return filtered
