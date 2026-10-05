"""Telegram interface (aiogram 3)."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .config import SLOTS, Settings
from .digest import build_deadlines, build_digest, build_nudge
from .lms.watcher import check_all
from .llm import LLMEngine
from .storage import Storage

log = logging.getLogger(__name__)
TG_LIMIT = 4000

COMMANDS = [
    BotCommand(command="digest", description="Digest now: /digest [morning|midday|evening]"),
    BotCommand(command="deadlines", description="Deadlines for the next 7 days"),
    BotCommand(command="nudge", description="Break a task into 5-minute steps: /nudge <task>"),
    BotCommand(command="lms", description="LMS watcher status"),
    BotCommand(command="check", description="Poll LMS portals now"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    for chunk in split_message(text):
        await bot.send_message(chat_id, chunk)


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, settings: Settings) -> None:
    owner = settings.telegram_chat_id
    log.info("/start from chat_id=%s", message.chat.id)
    if owner is None:
        await message.answer(
            f"👋 Your chat id: <code>{message.chat.id}</code>\n"
            "Put it into <code>TELEGRAM_CHAT_ID</code> in .env and restart the assistant."
        )
    elif message.chat.id == owner:
        await message.answer("✅ Assistant is running. Commands: /digest /deadlines /nudge /lms /check")
    else:
        await message.answer("This is a personal assistant bot.")


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject, settings: Settings,
                     db: Storage, llm: LLMEngine) -> None:
    slot = (command.args or "morning").strip().lower()
    if slot not in SLOTS:
        await message.answer(f"Slot must be one of: {', '.join(SLOTS)}")
        return
    await message.answer("⏳ Collecting…")
    result = await build_digest(slot, settings, db, llm)
    await send_long(message.bot, message.chat.id, result.text)
    await result.commit()


@router.message(Command("deadlines"))
async def cmd_deadlines(message: Message, settings: Settings, db: Storage) -> None:
    await send_long(message.bot, message.chat.id, await build_deadlines(settings, db))


@router.message(Command("nudge"))
async def cmd_nudge(message: Message, command: CommandObject, settings: Settings, llm: LLMEngine) -> None:
    if not command.args:
        await message.answer("Usage: /nudge <task>, e.g. <code>/nudge ML homework 3: linear regression report</code>")
        return
    await message.answer(await build_nudge(command.args.strip(), settings, llm))


@router.message(Command("lms"))
async def cmd_lms(message: Message, settings: Settings, db: Storage) -> None:
    if not settings.lms:
        await message.answer("No LMS configured (LMS_INSTANCES is empty).")
        return
    lines = ["<b>📚 LMS watcher</b>"]
    for inst in settings.lms:
        status = await db.get_kv(f"lms_status:{inst.name}") or "not checked yet"
        lines.append(f"• <b>{inst.name}</b> ({inst.kind}): {status}")
    await message.answer("\n".join(lines))


@router.message(Command("check"))
async def cmd_check(message: Message, settings: Settings, db: Storage) -> None:
    await message.answer("⏳ Checking LMS portals…")
    statuses = await check_all(settings, db)
    await message.answer("\n".join(f"• <b>{k}</b>: {v}" for k, v in statuses.items()) or "No LMS configured.")


def create_dispatcher(settings: Settings, storage: Storage, llm: LLMEngine) -> Dispatcher:
    dp = Dispatcher(settings=settings, db=storage, llm=llm)
    if settings.telegram_chat_id is not None:
        # Everything except /start is private to the owner
        router.message.filter((F.chat.id == settings.telegram_chat_id) | F.text.startswith("/start"))
    dp.include_router(router)
    return dp
