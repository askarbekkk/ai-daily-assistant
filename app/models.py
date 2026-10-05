"""Domain models (collected data) and LLM structured-output schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── Collected data ──────────────────────────────────────────


class Vacancy(BaseModel):
    id: str
    title: str
    company: str = ""
    url: str
    salary: str = ""
    area: str = ""
    experience: str = ""
    schedule: str = ""
    snippet: str = ""


class NewsItem(BaseModel):
    id: str
    title: str
    url: str
    source: str = ""
    published: datetime | None = None
    summary: str = ""


class Assignment(BaseModel):
    """An LMS task. `id` is namespaced by source, e.g. 'smart:1234'."""

    id: str
    source: str
    title: str
    course: str = ""
    due: datetime | None = None
    url: str = ""
    submitted: bool | None = None


ChangeKind = Literal["new", "due_changed", "removed"]


class AssignmentChange(BaseModel):
    kind: ChangeKind
    assignment: Assignment
    old_due: datetime | None = None


# ── LLM structured outputs ──────────────────────────────────


class JobPick(BaseModel):
    index: int = Field(description="Index of the vacancy in the provided list")
    why: str = Field(description="One short sentence: why it fits an ML/AI/Data/Backend profile")


class NewsPick(BaseModel):
    index: int = Field(description="Index of the news item in the provided list")
    summary: str = Field(description="1-2 factual sentences: what happened and why it matters; no hype")


class DigestLLM(BaseModel):
    """What the LLM writes. Deadlines are NOT here: they are rendered deterministically."""

    headline: str = Field(description="One-line focus of this part of the day, max 12 words")
    deadline_advice: str = Field(description="1-2 sentences: what to tackle first given deadlines; empty if none")
    lms_summary: str = Field(description="1-2 sentences summarising LMS changes; empty if none")
    jobs: list[JobPick] = Field(description="Only truly relevant vacancies, best first")
    news: list[NewsPick] = Field(description="Only substantive AI/ML news, best first")
    focus_tip: str = Field(description="One concrete, actionable tip for the next few hours")


class MicroStep(BaseModel):
    minutes: int = Field(description="Estimated minutes, 2-5")
    action: str = Field(description="Concrete action starting with a verb")


class NudgePlan(BaseModel):
    first_step: str = Field(description="The tiniest possible starting action (under 2 minutes)")
    steps: list[MicroStep] = Field(description="5-10 micro-steps of at most 5 minutes each")
    reward: str = Field(description="Small reward suggestion after finishing")
