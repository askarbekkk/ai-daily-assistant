from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ENV_FILE = ROOT / ".env"

SLOTS = ("morning", "midday", "evening")
LMS_KINDS = ("moodle", "canvas")


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class LMSInstance:
    """One LMS portal, configured via LMS_<NAME>_* variables."""

    name: str
    kind: str
    url: str
    token: str = ""
    username: str = ""
    password: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""
    # Selectors clicked in order before filling the form, e.g. an "SSO login" button and a login-type tab
    pre_login_clicks: tuple[str, ...] = ()
    # Optional selectors clicked after submitting if they appear, e.g. a "change password later" button
    post_login_clicks: tuple[str, ...] = ()

    @property
    def state_path(self) -> Path:
        return DATA_DIR / f"lms_{self.name}.json"

    @property
    def can_auto_login(self) -> bool:
        return all((self.username, self.password, self.username_selector, self.password_selector))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    telegram_bot_token: str = ""
    telegram_chat_id: int | None = None

    timezone: str = ""
    schedule: str = "09:00=morning,14:00=midday,21:00=evening"
    lms_poll_minutes: int = 60

    llm_provider: str = "none"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    # Tried in order when the main model is overloaded (503) or rate-limited (429)
    gemini_fallback_models: str = "gemini-flash-lite-latest,gemini-3.5-flash"
    digest_language: str = "ru"
    digest_max_words: int = 380

    hh_enabled: bool = True
    hh_app_token: str = ""
    hh_query: str = "machine learning OR data scientist OR ML engineer OR MLOps OR python backend"
    hh_area: str = ""
    hh_experience: str = ""
    hh_remote_only: bool = False
    job_include: str = (
        r"\bml\b|machine learning|машинн|\bai\b|\bии\b|data scien|nlp|computer vision|mlops|llm"
        r"|deep learning|нейросет|backend|бэкенд|python"
    )
    job_exclude: str = (
        r"senior|lead|head|principal|руководител|архитектор|продаж|sales|\b1[сc]\b|php|оператор"
        r"|менеджер|маркетолог|преподавател|учител|manager|marketing|recruit|account exec|customer success"
        r"|support|designer|writer|copywriter"
    )
    jobs_max: int = 5
    job_feeds: str = (
        "https://weworkremotely.com/categories/remote-programming-jobs.rss,"
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss,"
        "https://himalayas.app/jobs/rss"
    )
    job_feeds_lookback_days: int = 7

    news_feeds: str = (
        "https://huggingface.co/blog/feed.xml,https://openai.com/news/rss.xml,"
        "https://deepmind.google/blog/rss.xml,https://habr.com/ru/rss/hubs/machine_learning/articles/?fl=ru"
    )
    news_max: int = 5
    news_lookback_hours: int = 36

    lms_instances: str = ""
    lms_headless: bool = True

    db_path: Path = DATA_DIR / "assistant.db"

    @property
    def tz(self) -> ZoneInfo:
        if self.timezone:
            return ZoneInfo(self.timezone)
        from tzlocal import get_localzone

        return get_localzone()  # type: ignore[return-value]

    @property
    def feeds(self) -> list[str]:
        return _split(self.news_feeds)

    @property
    def job_feed_urls(self) -> list[str]:
        return _split(self.job_feeds)

    @property
    def schedule_slots(self) -> list[tuple[int, int, str]]:
        """Parse '09:00=morning,...' into [(9, 0, 'morning'), ...]."""
        return parse_schedule(self.schedule)

    @property
    def lms(self) -> list[LMSInstance]:
        return [load_lms_instance(name) for name in _split(self.lms_instances)]

    def lms_by_name(self, name: str) -> LMSInstance:
        for inst in self.lms:
            if inst.name == name:
                return inst
        raise KeyError(f"LMS instance {name!r} is not listed in LMS_INSTANCES")


def parse_schedule(value: str) -> list[tuple[int, int, str]]:
    result = []
    for item in _split(value):
        time_part, _, slot = item.partition("=")
        slot = slot.strip() or "morning"
        if slot not in SLOTS:
            raise ValueError(f"Unknown schedule slot {slot!r}; expected one of {SLOTS}")
        hour, minute = (int(x) for x in time_part.strip().split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"Bad time {time_part!r}")
        result.append((hour, minute, slot))
    return result


def _env_values() -> dict[str, str]:
    """Merge .env file and process env (process env wins), like pydantic-settings does."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        from dotenv import dotenv_values  # installed with pydantic-settings

        values.update({k: v for k, v in dotenv_values(ENV_FILE).items() if v})
    values.update({k: v for k, v in os.environ.items() if v})
    return values


def load_lms_instance(name: str) -> LMSInstance:
    env = _env_values()
    prefix = f"LMS_{name.upper()}_"

    def get(key: str, default: str = "") -> str:
        return env.get(prefix + key, default).strip()

    kind = get("KIND").lower()
    if kind not in LMS_KINDS:
        raise ValueError(f"{prefix}KIND must be one of {LMS_KINDS}, got {kind!r}")
    url = get("URL").rstrip("/")
    if not url:
        raise ValueError(f"{prefix}URL is required")
    return LMSInstance(
        name=name,
        kind=kind,
        url=url,
        token=get("TOKEN"),
        username=get("USERNAME"),
        password=get("PASSWORD"),
        username_selector=get("USERNAME_SELECTOR"),
        password_selector=get("PASSWORD_SELECTOR"),
        submit_selector=get("SUBMIT_SELECTOR"),
        # "|"-separated because CSS selectors themselves may contain commas
        pre_login_clicks=tuple(s.strip() for s in get("PRE_LOGIN_CLICKS").split("|") if s.strip()),
        post_login_clicks=tuple(s.strip() for s in get("POST_LOGIN_CLICKS").split("|") if s.strip()),
    )


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(exist_ok=True)
    return Settings()
