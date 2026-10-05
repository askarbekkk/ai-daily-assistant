"""Digest pipeline: collect → prioritise → LLM → render (Telegram HTML) → enforce word limit."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

import aiohttp

from .config import Settings
from .llm import LLMEngine, LLMUnavailable
from .models import Assignment, AssignmentChange, DigestLLM, NewsItem, NudgePlan, Vacancy
from .sources.hh import fetch_vacancies
from .sources.jobs_rss import fetch_rss_jobs
from .sources.news import fetch_news
from .storage import Storage

log = logging.getLogger(__name__)

# Which sections each slot carries
SLOT_SECTIONS = {
    "morning": {"overdue", "today", "tomorrow", "week", "changes", "jobs", "news"},
    "midday": {"overdue", "today", "changes", "jobs"},
    "evening": {"today", "tomorrow", "changes", "news"},
}
MAX_CANDIDATES = 15

L = {
    "ru": {
        "morning": "☀️ Утренний дайджест", "midday": "🕑 Дневная сводка", "evening": "🌙 Вечерняя проверка",
        "deadlines": "🔥 Дедлайны", "overdue": "просрочено", "today": "сегодня", "tomorrow": "завтра",
        "week": "на неделе", "changes": "📚 Изменения в LMS", "jobs": "💼 Вакансии", "news": "🧠 AI-новости",
        "tip": "🎯 Фокус", "new": "новое", "due_changed": "перенос", "removed": "удалено",
        "no_deadline": "без срока", "nothing": "Новых обновлений нет — хороший момент для глубокой работы.",
        "unavailable": "недоступно", "days": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
        "steps": "🪜 Микро-шаги", "start": "▶️ Начни с", "reward": "🎁 Награда",
    },
    "en": {
        "morning": "☀️ Morning digest", "midday": "🕑 Midday check", "evening": "🌙 Evening review",
        "deadlines": "🔥 Deadlines", "overdue": "overdue", "today": "today", "tomorrow": "tomorrow",
        "week": "this week", "changes": "📚 LMS changes", "jobs": "💼 Jobs", "news": "🧠 AI news",
        "tip": "🎯 Focus", "new": "new", "due_changed": "moved", "removed": "removed",
        "no_deadline": "no due date", "nothing": "Nothing new — a good window for deep work.",
        "unavailable": "unavailable", "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "steps": "🪜 Micro-steps", "start": "▶️ Start with", "reward": "🎁 Reward",
    },
}


def labels(settings: Settings) -> dict:
    return L.get(settings.digest_language, L["en"])


# ── deadlines (deterministic, never touched by the LLM) ─────


@dataclass
class DeadlineBuckets:
    overdue: list[Assignment] = field(default_factory=list)
    today: list[Assignment] = field(default_factory=list)
    tomorrow: list[Assignment] = field(default_factory=list)
    week: list[Assignment] = field(default_factory=list)

    def is_empty(self, keys: set[str]) -> bool:
        return not any(getattr(self, k) for k in ("overdue", "today", "tomorrow", "week") if k in keys)


def bucket_deadlines(assignments: list[Assignment], now: datetime, overdue_days: int = 3) -> DeadlineBuckets:
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_today = start_today + timedelta(days=1)
    end_tomorrow = end_today + timedelta(days=1)
    end_week = start_today + timedelta(days=7)
    b = DeadlineBuckets()
    for a in sorted((a for a in assignments if a.due and a.submitted is not True), key=lambda a: a.due):
        due = a.due.astimezone(now.tzinfo)
        if due < now:
            if due >= now - timedelta(days=overdue_days):
                b.overdue.append(a)
        elif due < end_today:
            b.today.append(a)
        elif due < end_tomorrow:
            b.tomorrow.append(a)
        elif due < end_week:
            b.week.append(a)
    return b


def fmt_due(due: datetime | None, now: datetime, lb: dict) -> str:
    if due is None:
        return lb["no_deadline"]
    d = due.astimezone(now.tzinfo)
    delta_days = (d.date() - now.date()).days
    hm = d.strftime("%H:%M")
    if delta_days == 0:
        return f"{lb['today']} {hm}"
    if delta_days == 1:
        return f"{lb['tomorrow']} {hm}"
    return f"{lb['days'][d.weekday()]} {d.strftime('%d.%m')} {hm}"


def _task_line(a: Assignment, now: datetime, lb: dict) -> str:
    title = f'<a href="{escape(a.url, quote=True)}">{escape(a.title)}</a>' if a.url else escape(a.title)
    course = f" · <i>{escape(a.course[:40])}</i>" if a.course else ""
    return f"• <b>{fmt_due(a.due, now, lb)}</b> — {title}{course}"


def render_deadlines(b: DeadlineBuckets, keys: set[str], now: datetime, lb: dict, week_limit: int = 5) -> list[str]:
    lines: list[str] = []
    for key in ("overdue", "today", "tomorrow", "week"):
        items = getattr(b, key)
        if key not in keys or not items:
            continue
        if key == "week":
            items = items[:week_limit]
        prefix = "⚠️ " if key == "overdue" else ""
        lines.append(f"{prefix}<u>{lb[key]}</u>")
        lines.extend(_task_line(a, now, lb) for a in items)
    return lines


def render_change(c: AssignmentChange, now: datetime, lb: dict) -> str:
    a = c.assignment
    title = escape(a.title)
    course = f" ({escape(a.course[:30])})" if a.course else ""
    if c.kind == "due_changed":
        return f"• {lb['due_changed']}: {title}{course}: {fmt_due(c.old_due, now, lb)} → <b>{fmt_due(a.due, now, lb)}</b>"
    if c.kind == "new":
        return f"• {lb['new']}: {title}{course} — <b>{fmt_due(a.due, now, lb)}</b>"
    return f"• {lb['removed']}: <s>{title}</s>{course}"


# ── word limit ──────────────────────────────────────────────


def visible_words(html_text: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html_text)
    return len(re.findall(r"[\w\-]+", text))


@dataclass
class Sections:
    header: str
    headline: str = ""
    deadlines: list[str] = field(default_factory=list)
    advice: str = ""
    changes: list[str] = field(default_factory=list)
    lms_summary: str = ""
    jobs: list[str] = field(default_factory=list)
    news: list[str] = field(default_factory=list)
    tip: str = ""
    footer: list[str] = field(default_factory=list)


def render(s: Sections, lb: dict) -> str:
    parts = [f"<b>{s.header}</b>"]
    if s.headline:
        parts.append(f"<i>{escape(s.headline)}</i>")
    if s.deadlines:
        block = [f"\n<b>{lb['deadlines']}</b>", *s.deadlines]
        if s.advice:
            block.append(f"💡 {escape(s.advice)}")
        parts.append("\n".join(block))
    if s.changes:
        block = [f"\n<b>{lb['changes']}</b>", *s.changes]
        if s.lms_summary:
            block.append(escape(s.lms_summary))
        parts.append("\n".join(block))
    if s.jobs:
        parts.append("\n".join([f"\n<b>{lb['jobs']}</b>", *s.jobs]))
    if s.news:
        parts.append("\n".join([f"\n<b>{lb['news']}</b>", *s.news]))
    if s.tip:
        parts.append(f"\n<b>{lb['tip']}:</b> {escape(s.tip)}")
    if s.footer:
        parts.append("\n<i>" + escape(" · ".join(s.footer)) + "</i>")
    return "\n".join(parts)


def trim_to_limit(s: Sections, lb: dict, max_words: int) -> str:
    """Drop lowest-priority content until the digest fits. Deadlines are never dropped."""
    text = render(s, lb)
    droppers = [
        lambda: s.news.pop() if s.news else None,
        lambda: s.jobs.pop() if len(s.jobs) > 1 else None,
        lambda: setattr(s, "lms_summary", "") if s.lms_summary else None,
        lambda: setattr(s, "headline", "") if s.headline else None,
        lambda: s.jobs.pop() if s.jobs else None,
        lambda: s.changes.pop() if len(s.changes) > 3 else None,
        lambda: setattr(s, "tip", "") if s.tip else None,
    ]
    while visible_words(text) > max_words:
        for drop in droppers:
            before = render(s, lb)
            drop()
            if render(s, lb) != before:
                break
        else:
            break  # only deadlines left
        text = render(s, lb)
    return text


# ── LLM prompt ──────────────────────────────────────────────


SYSTEM_PROMPT = """You are a concise personal assistant for an engineering student focused on ML, AI, \
Data Science, MLOps and Backend. Language: {lang}.
Rules:
- Facts only, no hype, no clickbait, no emojis inside fields.
- Jobs: keep only roles that fit ML/AI/Data Science/MLOps/Backend for a student or junior/middle engineer. \
Reject sales, support, teaching, senior/lead/head, unrelated stacks. Return at most {jobs_max}.
- News: keep only substantive items (model/tool releases, research, notable engineering posts). \
Skip marketing, webinars, listicles. Return at most {news_max}.
- Reference items only by their index. Every text field must be short.
- The whole digest including deadlines must fit in {max_words} words."""


def _user_prompt(slot: str, now: datetime, deadline_lines: list[str], changes: list[str],
                 jobs: list[Vacancy], news: list[NewsItem]) -> str:
    strip = lambda t: re.sub(r"<[^>]+>", "", t)  # noqa: E731
    out = [f"Slot: {slot}. Local time: {now:%A %Y-%m-%d %H:%M}."]
    out.append("Deadlines:\n" + ("\n".join(map(strip, deadline_lines)) or "none"))
    out.append("LMS changes:\n" + ("\n".join(map(strip, changes)) or "none"))
    if jobs:
        out.append("Vacancies:\n" + "\n".join(
            f"[{i}] {v.title} | {v.company} | {v.area} | {v.experience} | {v.schedule} | {v.salary} | {v.snippet[:160]}"
            for i, v in enumerate(jobs)))
    else:
        out.append("Vacancies: none (return empty list)")
    if news:
        out.append("News:\n" + "\n".join(
            f"[{i}] {n.title} ({n.source}) — {n.summary[:220]}" for i, n in enumerate(news)))
    else:
        out.append("News: none (return empty list)")
    return "\n\n".join(out)


def _fallback_llm(jobs: list[Vacancy], news: list[NewsItem], settings: Settings, has_deadlines: bool) -> DigestLLM:
    from .models import JobPick, NewsPick

    ru = settings.digest_language == "ru"
    return DigestLLM(
        headline="",
        deadline_advice=("Начни с ближайшего дедлайна." if ru else "Start with the nearest deadline.")
        if has_deadlines else "",
        lms_summary="",
        jobs=[JobPick(index=i, why=" · ".join(x for x in (v.experience, v.schedule, v.salary) if x))
              for i, v in enumerate(jobs[: settings.jobs_max])],
        news=[NewsPick(index=i, summary=(n.summary.split(". ")[0][:160] if n.summary else ""))
              for i, n in enumerate(news[: settings.news_max])],
        focus_tip=("Один 25-минутный блок без телефона на самую важную задачу."
                   if ru else "One 25-minute phone-free block on the most important task."),
    )


# ── pipeline ────────────────────────────────────────────────


@dataclass
class DigestResult:
    text: str
    empty: bool
    commit: Callable[[], Awaitable[None]]


async def _safe(coro, name: str, failures: list[str]):
    try:
        return await coro
    except Exception as exc:
        log.warning("%s failed: %s", name, exc)
        failures.append(name)
        return []


async def build_digest(slot: str, settings: Settings, storage: Storage, llm: LLMEngine) -> DigestResult:
    lb = labels(settings)
    keys = SLOT_SECTIONS[slot]
    now = datetime.now(settings.tz)
    failures: list[str] = []

    buckets = bucket_deadlines(await storage.all_assignments(), now)
    deadline_lines = render_deadlines(buckets, keys, now, lb)
    change_rows = await storage.unreported_changes()
    change_lines = [render_change(c, now, lb) for _, c in change_rows]

    async with aiohttp.ClientSession() as http:
        want_jobs = "jobs" in keys
        hh_task = (_safe(fetch_vacancies(settings, http), "HH.ru", failures)
                   if want_jobs and settings.hh_enabled else asyncio.sleep(0, []))
        rss_jobs_task = (_safe(fetch_rss_jobs(settings, http), "job RSS", failures)
                         if want_jobs and settings.job_feed_urls else asyncio.sleep(0, []))
        news_task = (_safe(fetch_news(settings, http), "RSS", failures)
                     if "news" in keys and settings.feeds else asyncio.sleep(0, []))
        hh_jobs, rss_jobs, all_news = await asyncio.gather(hh_task, rss_jobs_task, news_task)
    all_jobs = hh_jobs + rss_jobs

    unseen_jobs = await storage.filter_unseen("jobs", [v.id for v in all_jobs])
    jobs = [v for v in all_jobs if v.id in unseen_jobs][:MAX_CANDIDATES]
    unseen_news = await storage.filter_unseen("news", [n.id for n in all_news])
    news = [n for n in all_news if n.id in unseen_news][:MAX_CANDIDATES]

    has_content = bool(deadline_lines or change_lines or jobs or news)
    if has_content:
        try:
            ai = await llm.structured(
                SYSTEM_PROMPT.format(lang=settings.digest_language, jobs_max=settings.jobs_max,
                                     news_max=settings.news_max, max_words=settings.digest_max_words),
                _user_prompt(slot, now, deadline_lines, change_lines, jobs, news),
                DigestLLM,
            )
        except LLMUnavailable as exc:
            if llm.enabled:
                failures.append("LLM")
            log.info("Using template digest: %s", exc)
            ai = _fallback_llm(jobs, news, settings, bool(deadline_lines))
    else:
        ai = DigestLLM(headline="", deadline_advice="", lms_summary="", jobs=[], news=[], focus_tip="")

    s = Sections(header=f"{lb[slot]} · {now:%d.%m %H:%M}", headline=ai.headline,
                 deadlines=deadline_lines, advice=ai.deadline_advice if deadline_lines else "",
                 changes=change_lines, lms_summary=ai.lms_summary if change_lines else "",
                 tip=ai.focus_tip)
    for pick in _valid(ai.jobs, len(jobs), settings.jobs_max):
        v = jobs[pick.index]
        meta = " · ".join(x for x in (v.company, v.salary) if x)
        meta = f" — {escape(meta)}" if meta else ""
        why = f"\n   {escape(pick.why)}" if pick.why else ""
        s.jobs.append(f'• <a href="{escape(v.url, quote=True)}">{escape(v.title)}</a>{meta}{why}')
    for pick in _valid(ai.news, len(news), settings.news_max):
        n = news[pick.index]
        summary = f" — {escape(pick.summary)}" if pick.summary else ""
        s.news.append(f'• <a href="{escape(n.url, quote=True)}">{escape(n.title)}</a>{summary}')
    if not has_content:
        s.tip = lb["nothing"]
    if failures:
        s.footer.append(f"{lb['unavailable']}: {', '.join(failures)}")

    text = trim_to_limit(s, lb, settings.digest_max_words)

    async def commit() -> None:
        # All candidates count as seen: shown ones shouldn't repeat, rejected ones are irrelevant
        await storage.mark_seen("jobs", [v.id for v in jobs])
        await storage.mark_seen("news", [n.id for n in news])
        await storage.mark_changes_reported([i for i, _ in change_rows])

    return DigestResult(text=text, empty=not has_content, commit=commit)


def _valid(picks, n: int, limit: int):
    seen: set[int] = set()
    for p in picks:
        if 0 <= p.index < n and p.index not in seen:
            seen.add(p.index)
            yield p
            if len(seen) >= limit:
                return


async def build_deadlines(settings: Settings, storage: Storage) -> str:
    lb = labels(settings)
    now = datetime.now(settings.tz)
    b = bucket_deadlines(await storage.all_assignments(), now)
    lines = render_deadlines(b, {"overdue", "today", "tomorrow", "week"}, now, lb, week_limit=20)
    return "\n".join([f"<b>{lb['deadlines']}</b>", *lines]) if lines else f"<b>{lb['deadlines']}</b>\n—"


# ── v1.2 Cognitive Nudge ────────────────────────────────────

NUDGE_PROMPT = """You help a student beat procrastination. Language: {lang}.
Break the task into 5-10 concrete micro-steps, each at most 5 minutes, each starting with a verb and \
physically doable right now. The first step must take under 2 minutes and need zero willpower. \
No motivational fluff."""


async def build_nudge(task: str, settings: Settings, llm: LLMEngine) -> str:
    lb = labels(settings)
    try:
        plan = await llm.structured(NUDGE_PROMPT.format(lang=settings.digest_language), f"Task: {task}", NudgePlan)
    except LLMUnavailable:
        ru = settings.digest_language == "ru"
        from .models import MicroStep

        generic = (
            ["Открой файл/страницу задания", "Выпиши, что именно нужно сдать", "Разбей на 3 части",
             "Сделай самую простую часть черново", "Проверь и сохрани прогресс"]
            if ru else
            ["Open the task file/page", "Write down what exactly must be delivered", "Split it into 3 parts",
             "Draft the easiest part", "Review and save progress"]
        )
        plan = NudgePlan(
            first_step=generic[0],
            steps=[MicroStep(minutes=5, action=a) for a in generic],
            reward="5 минут перерыва" if ru else "a 5-minute break",
        )
    lines = [f"<b>🧩 {escape(task)}</b>", f"\n<b>{lb['start']}:</b> {escape(plan.first_step)}", f"\n<b>{lb['steps']}</b>"]
    lines += [f"{i}. {escape(s.action)} <i>({s.minutes}′)</i>" for i, s in enumerate(plan.steps, 1)]
    lines.append(f"\n<b>{lb['reward']}:</b> {escape(plan.reward)}")
    return "\n".join(lines)
