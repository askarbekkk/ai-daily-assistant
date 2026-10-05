"""Cron-scheduled digests and interval LMS polling."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .bot import send_long
from .config import Settings
from .digest import build_digest
from .lms.watcher import check_all
from .llm import LLMEngine
from .storage import Storage

log = logging.getLogger(__name__)


async def run_digest(slot: str, settings: Settings, storage: Storage, llm: LLMEngine, bot: Bot) -> None:
    if settings.lms:
        await check_all(settings, storage, notify=lambda t: bot.send_message(settings.telegram_chat_id, t))
    result = await build_digest(slot, settings, storage, llm)
    if result.empty and slot != "morning":
        log.info("Slot %s: nothing new, skipping message", slot)
        return
    await send_long(bot, settings.telegram_chat_id, result.text)
    await result.commit()
    log.info("Slot %s digest sent", slot)


async def run_lms_poll(settings: Settings, storage: Storage, bot: Bot) -> None:
    await check_all(settings, storage, notify=lambda t: bot.send_message(settings.telegram_chat_id, t))


def create_scheduler(settings: Settings, storage: Storage, llm: LLMEngine, bot: Bot) -> AsyncIOScheduler:
    tz = settings.tz
    scheduler = AsyncIOScheduler(timezone=tz, job_defaults={"coalesce": True, "misfire_grace_time": 3600})
    for hour, minute, slot in settings.schedule_slots:
        scheduler.add_job(
            run_digest, CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[slot, settings, storage, llm, bot], id=f"digest-{slot}-{hour:02d}{minute:02d}",
            max_instances=1,
        )
        log.info("Scheduled %s digest at %02d:%02d (%s)", slot, hour, minute, tz)
    if settings.lms:
        scheduler.add_job(
            run_lms_poll, IntervalTrigger(minutes=settings.lms_poll_minutes, timezone=tz),
            args=[settings, storage, bot], id="lms-poll", max_instances=1,
            next_run_time=datetime.now(tz) + timedelta(seconds=20),
        )
        log.info("LMS watcher every %d min for: %s", settings.lms_poll_minutes,
                 ", ".join(i.name for i in settings.lms))
    return scheduler
