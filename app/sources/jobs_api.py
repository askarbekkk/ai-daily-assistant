"""Keyless remote-job JSON APIs (Jobicy, RemoteOK), filtered with the same title regexes as HH.ru."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from ..config import Settings
from ..models import Vacancy
from .hh import filter_vacancies
from .news import _text

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-daily-assistant/1.0)"}
JOBICY_QUERIES = ("industry=data-science", "tag=python", "tag=machine-learning")


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


async def _remoteok(session, since):
    return parse_remoteok(await _get_json(session, "https://remoteok.com/api"), since)


async def fetch_api_jobs(settings: Settings, session: aiohttp.ClientSession) -> list[Vacancy]:
    since = datetime.now(timezone.utc) - timedelta(days=settings.job_feeds_lookback_days)
    enabled = {s.strip().lower() for s in settings.job_apis.split(",") if s.strip()}
    tasks = []
    if "jobicy" in enabled:
        tasks.append(_safe(_jobicy(session, since), "jobicy"))
    if "remoteok" in enabled:
        tasks.append(_safe(_remoteok(session, since), "remoteok"))
    items = [v for r in await asyncio.gather(*tasks) for v in r]
    unique = list({v.id: v for v in items}.values())
    filtered = filter_vacancies(unique, settings.job_include, settings.job_exclude)
    log.info("Job APIs: %d fetched, %d after filter", len(unique), len(filtered))
    return filtered
