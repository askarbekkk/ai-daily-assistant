"""CLI entry point: python -m app <command>."""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
import sys
from datetime import datetime

from .config import SLOTS, get_settings
from .llm import LLMEngine
from .storage import Storage


def to_console(text: str) -> str:
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def cmd_run() -> None:
    from .bot import COMMANDS, create_bot, create_dispatcher
    from .scheduler import create_scheduler

    settings = get_settings()
    if not settings.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set in .env")
    storage = Storage(settings.db_path)
    await storage.init()
    llm = LLMEngine(settings)
    bot = create_bot(settings)
    dp = create_dispatcher(settings, storage, llm)
    await bot.set_my_commands(COMMANDS)
    if settings.telegram_chat_id is None:
        logging.warning("TELEGRAM_CHAT_ID not set: send /start to the bot to get it. Scheduler is disabled.")
    else:
        create_scheduler(settings, storage, llm, bot).start()
    logging.info("LLM: %s (%s)", settings.llm_provider, "on" if llm.enabled else "template fallback")
    await dp.start_polling(bot)


async def cmd_digest(slot: str, dry_run: bool, refresh_lms: bool, skip_empty: bool = False) -> None:
    from .digest import build_digest
    from .lms.watcher import check_all

    settings = get_settings()
    storage = Storage(settings.db_path)
    await storage.init()
    if slot == "auto":
        slot = slot_for_hour(datetime.now(settings.tz).hour)
        print(f"Slot: {slot}")

    if dry_run:
        if refresh_lms and settings.lms:
            print(json.dumps(await check_all(settings, storage), ensure_ascii=False, indent=2))
        result = await build_digest(slot, settings, storage, LLMEngine(settings))
        print(to_console(result.text))
        from .digest import visible_words

        print(f"\n--- {visible_words(result.text)} words (limit {settings.digest_max_words}); dry run, nothing marked as seen")
        return

    from .bot import create_bot, send_long

    if not settings.telegram_bot_token or settings.telegram_chat_id is None:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run")
    bot = create_bot(settings)
    try:
        if refresh_lms and settings.lms:
            notify = lambda t: bot.send_message(settings.telegram_chat_id, t)  # noqa: E731
            print(json.dumps(await check_all(settings, storage, notify=notify), ensure_ascii=False, indent=2))
        result = await build_digest(slot, settings, storage, LLMEngine(settings))
        if skip_empty and result.empty and slot != "morning":
            print(f"Slot {slot}: nothing new, not sending.")
            return
        await send_long(bot, settings.telegram_chat_id, result.text)
        await result.commit()
        print("Sent.")
    finally:
        await bot.session.close()


def slot_for_hour(hour: int) -> str:
    if hour < 12:
        return "morning"
    if hour < 18:
        return "midday"
    return "evening"


async def cmd_lms(action: str, name: str | None) -> None:
    from .lms import make_adapter
    from .lms.watcher import check_all

    settings = get_settings()
    if action == "check":
        storage = Storage(settings.db_path)
        await storage.init()
        print(json.dumps(await check_all(settings, storage), ensure_ascii=False, indent=2))
        for a in sorted((a for a in await storage.all_assignments() if a.due), key=lambda a: a.due)[:15]:
            print(f"  {a.due.astimezone(settings.tz):%a %d.%m %H:%M}  [{a.source}] {a.title} — {a.course}")
        return
    adapter = make_adapter(settings.lms_by_name(name), headless=settings.lms_headless)
    if action == "login":
        await adapter.interactive_login()
    elif action == "probe":
        print(json.dumps(await adapter.probe(), ensure_ascii=False, indent=2))


async def cmd_nudge(task: str) -> None:
    from .digest import build_nudge

    settings = get_settings()
    print(to_console(await build_nudge(task, settings, LLMEngine(settings))))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    p = argparse.ArgumentParser(prog="python -m app", description="Personal AI Daily Assistant & LMS Watcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Start Telegram bot + scheduler")
    d = sub.add_parser("digest", help="Build a digest once")
    d.add_argument("slot", choices=(*SLOTS, "auto"), nargs="?", default="morning",
                   help="'auto' picks the slot from the current local time")
    d.add_argument("--dry-run", action="store_true", help="Print to console instead of sending")
    d.add_argument("--refresh-lms", action="store_true", help="Poll LMS portals first")
    d.add_argument("--skip-empty", action="store_true", help="Don't send midday/evening digests with nothing new")
    for name, help_ in (("lms-login", "Log in to an LMS in a visible browser and save the session"),
                        ("lms-probe", "Show login form fields (to configure auto-login)")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("name", help="Instance name from LMS_INSTANCES, e.g. smart")
    sub.add_parser("lms-check", help="Poll all LMS portals now and show upcoming tasks")
    n = sub.add_parser("nudge", help="Break a task into 5-minute micro-steps")
    n.add_argument("task")
    args = p.parse_args()

    if args.cmd == "run":
        coro = cmd_run()
    elif args.cmd == "digest":
        coro = cmd_digest(args.slot, args.dry_run, args.refresh_lms, args.skip_empty)
    elif args.cmd == "lms-check":
        coro = cmd_lms("check", None)
    elif args.cmd in ("lms-login", "lms-probe"):
        coro = cmd_lms(args.cmd.removeprefix("lms-"), args.name)
    else:
        coro = cmd_nudge(args.task)
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
