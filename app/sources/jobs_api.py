"""Keyless job sources: Korea (Wanted, LinkedIn guest search) and remote (Jobicy, RemoteOK).
All filtered with the same title regexes as HH.ru."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from bs4 import BeautifulSoup

from ..config import Settings
from ..models import Vacancy
from .hh import filter_vacancies
from .news import _text

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-daily-assistant/1.0)"}
JOBICY_QUERIES = ("industry=data-science", "tag=python", "tag=machine-learning")
# Wanted tags: 1634 ML engineer, 1024 data scientist, 872 server/backend; years=0 = open to newcomers
WANTED_TAGS = (1634, 1024, 872)
# LinkedIn: f_E=1,2 internship + entry level, f_TPR=r604800 posted in the last week
LINKEDIN_QUERIES = ("machine learning", "data scientist", "AI engineer", "python backend")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _salary(lo, hi, currency: str = "") -> str:
    lo, hi = int(lo or 0), int(hi or 0)
    if lo and hi:
        return f"{lo:,}–{hi:,} {currency}".replace(",", " ").strip()
    if lo or hi:
        return f"{lo or hi:,} {currency}".replace(",", " ").strip()
    return ""


def parse_jobicy(data: dict, since: datetime) -> list[Vacancy]:
    out = []
    for j in data.get("jobs", []):
        published = _dt(j.get("pubDate"))
        if published and published < since:
            continue
        out.append(Vacancy(
            id=f"jobicy:{j['id']}",
            title=(j.get("jobTitle") or "").strip(),
            company=j.get("companyName") or "",
            url=j.get("url") or "",
            salary=_salary(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency") or ""),
            area=j.get("jobGeo") or "",
            experience=j.get("jobLevel") or "",
            schedule="remote",
            snippet=_text(j.get("jobExcerpt") or "", 300),
        ))
    return out


def parse_remoteok(data: list, since: datetime) -> list[Vacancy]:
    out = []
    for j in data[1:]:  # first element is the API legal notice
        published = _dt(j.get("date"))
        if published and published < since:
            continue
        out.append(Vacancy(
            id=f"remoteok:{j['id']}",
            title=(j.get("position") or "").strip(),
            company=j.get("company") or "",
            url=j.get("url") or "",
            salary=_salary(j.get("salary_min"), j.get("salary_max"), "USD"),
            area=j.get("location") or "",
            schedule="remote",
            snippet=_text(j.get("description") or "", 300),
        ))
    return out


def parse_wanted(data: dict) -> list[Vacancy]:
    # no publish date in the API; results are newest-first and the digest de-dups already seen ids
    out = []
    for j in data.get("data", []):
        lo, hi = j.get("annual_from"), j.get("annual_to")
        out.append(Vacancy(
            id=f"wanted:{j['id']}",
            title=(j.get("position") or "").strip(),
            company=(j.get("company") or {}).get("name") or "",
            url=f"https://www.wanted.co.kr/wd/{j['id']}",
            area=(j.get("address") or {}).get("location") or "Korea",
            experience=f"{lo}–{hi} yrs" if hi else "",
            schedule=f"until {j['due_time']}" if j.get("due_time") else "",
        ))
    return out


def parse_linkedin(html: str) -> list[Vacancy]:
    out = []
    for card in BeautifulSoup(html, "html.parser").select("div.base-card[data-entity-urn]"):
        title, link = card.select_one(".base-search-card__title"), card.select_one("a.base-card__full-link")
        if not title or not link:
            continue
        company = card.select_one(".base-search-card__subtitle")
        area = card.select_one(".job-search-card__location")
        out.append(Vacancy(
            id="linkedin:" + card["data-entity-urn"].rsplit(":", 1)[-1],
            title=title.get_text(strip=True),
            company=company.get_text(strip=True) if company else "",
            url=link["href"].split("?", 1)[0],
            area=area.get_text(strip=True) if area else "South Korea",
            experience="intern/entry",
        ))
    return out


async def _get_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=25)) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def _safe(coro, name: str) -> list[Vacancy]:
    try:
        return await coro
    except Exception as exc:  # one broken source must not break the digest
        log.warning("Job API %s failed: %s", name, exc)
        return []


async def _jobicy(session, since):
    results = await asyncio.gather(*(
        _get_json(session, f"https://jobicy.com/api/v2/remote-jobs?count=50&{q}") for q in JOBICY_QUERIES
    ))
    return [v for data in results for v in parse_jobicy(data, since)]


async def _wanted(session):
    results = await asyncio.gather(*(_get_json(
        session, "https://www.wanted.co.kr/api/v4/jobs?country=kr&locations=all&years=0"
                 f"&job_sort=job.latest_order&limit=20&tag_type_ids={t}") for t in WANTED_TAGS))
    return [v for data in results for v in parse_wanted(data)]


async def _linkedin_one(session, query: str) -> str:
    async with session.get(
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
        params={"keywords": query, "location": "South Korea", "f_E": "1,2", "f_TPR": "r604800"},
        headers=HEADERS, timeout=aiohttp.ClientTimeout(total=25),
    ) as r:
        r.raise_for_status()
        return await r.text()


async def _linkedin(session):
    pages = await asyncio.gather(*(_linkedin_one(session, q) for q in LINKEDIN_QUERIES))
    return [v for html in pages for v in parse_linkedin(html)]


async def _remoteok(session, since):
    return parse_remoteok(await _get_json(session, "https://remoteok.com/api"), since)


async def fetch_api_jobs(settings: Settings, session: aiohttp.ClientSession) -> list[Vacancy]:
    since = datetime.now(timezone.utc) - timedelta(days=settings.job_feeds_lookback_days)
    enabled = {s.strip().lower() for s in settings.job_apis.split(",") if s.strip()}
    tasks = []  # Korea first: the digest caps candidates, so earlier sources win
    if "wanted" in enabled:
        tasks.append(_safe(_wanted(session), "wanted"))
    if "linkedin" in enabled:
        tasks.append(_safe(_linkedin(session), "linkedin"))
    if "jobicy" in enabled:
        tasks.append(_safe(_jobicy(session, since), "jobicy"))
    if "remoteok" in enabled:
        tasks.append(_safe(_remoteok(session, since), "remoteok"))
    items = [v for r in await asyncio.gather(*tasks) for v in r]
    unique = list({v.id: v for v in items}.values())
    filtered = filter_vacancies(unique, settings.job_include, settings.job_exclude)
    log.info("Job APIs: %d fetched, %d after filter", len(unique), len(filtered))
    return filtered
