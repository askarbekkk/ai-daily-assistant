"""Moodle adapter: uses Moodle's own AJAX web service from inside the logged-in page.

core_calendar_get_action_events_by_timesort is what the Dashboard "Timeline" block uses,
so it is available on any Moodle 3.3+ without admin-enabled web services or tokens.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.async_api import Page

from ..models import Assignment
from .base import LMSAdapter

LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 90

_SUFFIX = re.compile(r"\s+(is due|should be completed|closes|마감|종료|제출 기한)\s*$", re.IGNORECASE)

FETCH_JS = """
async ({from, to}) => {
  if (typeof M === 'undefined' || !M.cfg || !M.cfg.sesskey) return {error: 'no sesskey'};
  const method = 'core_calendar_get_action_events_by_timesort';
  const body = [{index: 0, methodname: method, args: {
      limitnum: 50, timesortfrom: from, timesortto: to, limittononsuspendedevents: true}}];
  const r = await fetch(`${M.cfg.wwwroot}/lib/ajax/service.php?sesskey=${M.cfg.sesskey}&info=${method}`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return await r.json();
}
"""


def parse_events(events: list[dict], source: str) -> list[Assignment]:
    items = []
    for ev in events:
        ts = ev.get("timesort") or ev.get("timestart")
        items.append(
            Assignment(
                id=f"{source}:{ev['id']}",
                source=source,
                title=_SUFFIX.sub("", (ev.get("activityname") or ev.get("name") or "").strip()),
                course=((ev.get("course") or {}).get("fullname") or "").strip(),
                due=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                url=ev.get("url") or "",
                submitted=None,  # action events disappear once the task is completed
            )
        )
    return items


class MoodleAdapter(LMSAdapter):
    @property
    def login_url(self) -> str:
        return f"{self.inst.url}/login/index.php"

    @property
    def home_url(self) -> str:
        return f"{self.inst.url}/my/"

    async def is_logged_in(self, page: Page) -> bool:
        if urlparse(page.url).netloc != urlparse(self.inst.url).netloc or "/login/" in page.url:
            return False
        return await page.evaluate(
            """() => typeof M !== 'undefined' && !!(M.cfg && M.cfg.sesskey)
                     && !document.body.classList.contains('notloggedin')"""
        )

    async def collect(self, page: Page) -> list[Assignment]:
        now = int(time.time())
        result = await page.evaluate(
            FETCH_JS, {"from": now - LOOKBACK_DAYS * 86400, "to": now + LOOKAHEAD_DAYS * 86400}
        )
        if isinstance(result, dict):
            raise RuntimeError(f"Moodle: {result.get('error')}")
        first = result[0]
        if first.get("error"):
            exc = first.get("exception") or {}
            raise RuntimeError(f"Moodle AJAX error: {exc.get('errorcode')} {exc.get('message')}")
        return parse_events(first["data"].get("events", []), self.inst.name)
