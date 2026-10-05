"""HH.ru vacancies via the public API, with strict title-based filtering."""
from __future__ import annotations

import logging
import re

import aiohttp

from ..config import Settings
from ..models import Vacancy

log = logging.getLogger(__name__)

API_URL = "https://api.hh.ru/vacancies"
USER_AGENT = "ai-daily-assistant/1.0 (personal digest bot)"


def _salary(raw: dict | None) -> str:
    if not raw:
        return ""
    lo, hi, cur = raw.get("from"), raw.get("to"), raw.get("currency") or ""
    if lo and hi:
        return f"{lo:,}–{hi:,} {cur}".replace(",", " ")
    if lo:
        return f"от {lo:,} {cur}".replace(",", " ")
    if hi:
        return f"до {hi:,} {cur}".replace(",", " ")
    return ""


def _clean(text: str | None) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_vacancy(item: dict) -> Vacancy:
    snippet = item.get("snippet") or {}
    return Vacancy(
        id=f"hh:{item['id']}",
        title=item.get("name", ""),
        company=(item.get("employer") or {}).get("name", ""),
        url=item.get("alternate_url", ""),
        salary=_salary(item.get("salary")),
        area=(item.get("area") or {}).get("name", ""),
        experience=(item.get("experience") or {}).get("name", ""),
        schedule=(item.get("schedule") or {}).get("name", ""),
        snippet=_clean(snippet.get("requirement"))[:300],
    )


def filter_vacancies(items: list[Vacancy], include: str, exclude: str) -> list[Vacancy]:
    inc = re.compile(include, re.IGNORECASE) if include else None
    exc = re.compile(exclude, re.IGNORECASE) if exclude else None
    out = []
    for v in items:
        if exc and exc.search(v.title):
            continue
        if inc and not inc.search(v.title):
            continue
        out.append(v)
    return out


async def fetch_vacancies(settings: Settings, session: aiohttp.ClientSession) -> list[Vacancy]:
    # Since April 2026 /vacancies returns 403 without an application token
    if not settings.hh_app_token:
        log.info("HH.ru skipped: HH_APP_TOKEN is not set (register an app at https://dev.hh.ru)")
        return []
    params: dict[str, str] = {
        "text": settings.hh_query,
        "search_field": "name",
        "period": "1",
        "order_by": "publication_time",
        "per_page": "100",
    }
    if settings.hh_area:
        params["area"] = settings.hh_area
    if settings.hh_experience:
        params["experience"] = settings.hh_experience
    if settings.hh_remote_only:
        params["schedule"] = "remote"

    headers = {
        "User-Agent": USER_AGENT,
        "HH-User-Agent": USER_AGENT,
        "Authorization": f"Bearer {settings.hh_app_token}",
    }
    async with session.get(API_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            body = (await r.text())[:200]
            raise RuntimeError(f"HH.ru API returned {r.status}: {body}")
        data = await r.json()

    vacancies = [parse_vacancy(i) for i in data.get("items", [])]
    filtered = filter_vacancies(vacancies, settings.job_include, settings.job_exclude)
    log.info("HH.ru: %d fetched, %d after filter", len(vacancies), len(filtered))
    return filtered
