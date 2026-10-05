"""Polls every configured LMS, stores snapshots and detected changes."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ..config import Settings
from ..storage import Storage
from . import SessionExpired, make_adapter
from .diff import diff_assignments

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


async def check_all(settings: Settings, storage: Storage, notify: Notify | None = None) -> dict[str, str]:
    """Return a human-readable status per instance."""
    statuses: dict[str, str] = {}
    for inst in settings.lms:
        now = datetime.now(timezone.utc).isoformat(timespec="minutes")
        try:
            items = await make_adapter(inst, headless=settings.lms_headless).fetch()
            first_run = not await storage.has_snapshot(inst.name)
            old = await storage.load_snapshot(inst.name)
            # First run only builds a baseline: no flood of "new" notifications
            changes = [] if first_run else diff_assignments(old, items)
            await storage.save_snapshot(inst.name, items)
            await storage.add_changes(changes)
            await storage.delete_kv(f"session_alert:{inst.name}")
            status = f"ok · {len(items)} tasks · {len(changes)} changes · {now}"
            log.info("[%s] %s", inst.name, status)
        except SessionExpired as exc:
            status = f"session expired · {now}"
            log.warning("[%s] %s", inst.name, exc)
            if notify and not await storage.get_kv(f"session_alert:{inst.name}"):
                await notify(f"🔐 {exc}")
                await storage.set_kv(f"session_alert:{inst.name}", now)
        except Exception as exc:
            status = f"error: {exc.__class__.__name__}: {str(exc)[:150]} · {now}"
            log.exception("[%s] LMS check failed", inst.name)
        statuses[inst.name] = status
        await storage.set_kv(f"lms_status:{inst.name}", status)
    return statuses
