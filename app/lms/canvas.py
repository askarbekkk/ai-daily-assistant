"""Canvas adapter: Planner API, authenticated by API token or by the saved browser session."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import aiohttp
from playwright.async_api import Page

from ..models import Assignment
from .base import LMSAdapter

LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 90
TASK_TYPES = {"assignment", "quiz", "discussion_topic", "calendar_event", "planner_note", "assessment_request"}


def _loads(text: str):
    # Canvas prefixes session-authenticated JSON with "while(1);" against JSON hijacking
    return json.loads(text.removeprefix("while(1);"))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_planner_items(items: list[dict], source: str, base_url: str) -> list[Assignment]:
    out = []
    for it in items:
        ptype = it.get("plannable_type", "")
        if ptype not in TASK_TYPES:
            continue
        plannable = it.get("plannable") or {}
        subs = it.get("submissions")
        submitted = None
        if isinstance(subs, dict):
            submitted = bool(subs.get("submitted") or subs.get("excused") or subs.get("graded"))
        state = plannable.get("workflow_state")
        if ptype == "assessment_request" and state:
            submitted = state == "completed"
        if (it.get("planner_override") or {}).get("marked_complete"):
            submitted = True
        title = (plannable.get("title") or plannable.get("name") or "").strip()
        if ptype == "assessment_request":
            title = f"Peer review: {title}"
        due = _dt(plannable.get("due_at") or plannable.get("todo_date") or it.get("plannable_date"))
        out.append(
            Assignment(
                id=f"{source}:{ptype}:{it.get('plannable_id')}",
                source=source,
                title=title,
                course=(it.get("context_name") or "").strip(),
                due=due,
                url=urljoin(base_url + "/", it.get("html_url") or ""),
                submitted=submitted,
            )
        )
    return out


def _planner_params() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "start_date": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_page": "100",
    }


class CanvasAdapter(LMSAdapter):
    @property
    def login_url(self) -> str:
        return f"{self.inst.url}/login"

    @property
    def home_url(self) -> str:
        return f"{self.inst.url}/"

    async def fetch(self) -> list[Assignment]:
        if self.inst.token:
            return await self._fetch_with_token()
        return await super().fetch()

    async def _fetch_with_token(self) -> list[Assignment]:
        headers = {"Authorization": f"Bearer {self.inst.token}"}
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{self.inst.url}/api/v1/planner/items", params=_planner_params()) as r:
                if r.status == 401:
                    raise RuntimeError(f"Canvas token for '{self.inst.name}' is invalid or expired")
                r.raise_for_status()
                items = _loads(await r.text())
        return parse_planner_items(items, self.inst.name, self.inst.url)

    async def is_logged_in(self, page: Page) -> bool:
        if urlparse(page.url).netloc != urlparse(self.inst.url).netloc or "/login" in page.url:
            return False
        r = await page.context.request.get(f"{self.inst.url}/api/v1/users/self")
        return r.ok

    async def collect(self, page: Page) -> list[Assignment]:
        r = await page.context.request.get(f"{self.inst.url}/api/v1/planner/items", params=_planner_params())
        if not r.ok:
            raise RuntimeError(f"Canvas planner API returned {r.status}")
        return parse_planner_items(_loads(await r.text()), self.inst.name, self.inst.url)
