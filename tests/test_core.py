from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import parse_schedule
from app.digest import L, Sections, bucket_deadlines, render, trim_to_limit, visible_words
from app.lms.canvas import _loads, parse_planner_items
from app.lms.diff import diff_assignments
from app.lms.moodle import parse_events
from app.models import Assignment, Vacancy
from app.sources.hh import filter_vacancies
from app.sources.news import is_clickbait
from app.storage import Storage

TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)


def task(i: str, due: datetime | None, **kw) -> Assignment:
    return Assignment(id=f"smart:{i}", source="smart", title=f"Task {i}", due=due, **kw)


# ── diff ────────────────────────────────────────────────────


def test_diff_detects_new_moved_removed():
    old = {a.id: a for a in [task("1", NOW), task("2", NOW), task("3", NOW)]}
    new = [task("1", NOW + timedelta(seconds=30)), task("2", NOW + timedelta(days=2)), task("4", NOW)]
    kinds = {(c.kind, c.assignment.id) for c in diff_assignments(old, new)}
    assert kinds == {("due_changed", "smart:2"), ("removed", "smart:3"), ("new", "smart:4")}


def test_diff_due_added():
    old = {"smart:1": task("1", None)}
    changes = diff_assignments(old, [task("1", NOW)])
    assert [c.kind for c in changes] == ["due_changed"] and changes[0].old_due is None


# ── deadlines ───────────────────────────────────────────────


def test_buckets_and_submitted_skipped():
    items = [
        task("od", NOW - timedelta(hours=5)),
        task("old", NOW - timedelta(days=10)),
        task("today", NOW.replace(hour=23, minute=59)),
        task("tom", NOW + timedelta(days=1)),
        task("wk", NOW + timedelta(days=4)),
        task("far", NOW + timedelta(days=20)),
        task("done", NOW.replace(hour=20), submitted=True),
        task("nodue", None),
    ]
    b = bucket_deadlines(items, NOW)
    assert [a.id for a in b.overdue] == ["smart:od"]
    assert [a.id for a in b.today] == ["smart:today"]
    assert [a.id for a in b.tomorrow] == ["smart:tom"]
    assert [a.id for a in b.week] == ["smart:wk"]


def test_utc_due_bucketed_in_local_time():
    # 15:30 UTC = 00:30 next day in Seoul → tomorrow, not today
    due = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
    b = bucket_deadlines([task("x", due)], NOW)
    assert b.tomorrow and not b.today


# ── word limit ──────────────────────────────────────────────


def test_trim_keeps_deadlines_and_fits():
    lb = L["ru"]
    s = Sections(
        header="Дайджест",
        headline="слово " * 10,
        deadlines=["• <b>сегодня 23:59</b> — Важное задание"],
        jobs=[f'• <a href="https://x/{i}">Вакансия {i}</a> — ' + "описание " * 20 for i in range(5)],
        news=[f'• <a href="https://y/{i}">Новость {i}</a> — ' + "текст " * 30 for i in range(5)],
        tip="совет " * 10,
    )
    assert visible_words(render(s, lb)) > 100
    text = trim_to_limit(s, lb, 100)
    assert visible_words(text) <= 100
    assert "Важное задание" in text
    assert text.index("Дедлайны") < text.index("Вакансии") if "Вакансии" in text else True


def test_visible_words_ignores_urls():
    assert visible_words('<a href="https://very.long/url/with/many/parts">two words</a>') == 2


# ── filters ─────────────────────────────────────────────────


def test_job_filter():
    vs = [Vacancy(id=str(i), title=t, url="u") for i, t in enumerate(
        ["ML Engineer", "Senior Data Scientist", "Менеджер по продажам AI", "Python Backend Developer",
         "Junior MLOps", "Бухгалтер 1С", "Data Scientist (NLP)"])]
    inc = r"\bml\b|data scien|mlops|backend|python|nlp"
    exc = r"senior|lead|продаж|\b1[сc]\b|менеджер"
    assert [v.title for v in filter_vacancies(vs, inc, exc)] == [
        "ML Engineer", "Python Backend Developer", "Junior MLOps", "Data Scientist (NLP)"]


@pytest.mark.parametrize("title,expected", [
    ("Introducing Gemma 4: open models", False),
    ("You won't believe what this AI did", True),
    ("Топ-10 нейросетей для студентов", True),
    ("Wow!! New model!!", True),
    ("Fine-tuning LLMs with LoRA on a single GPU", False),
])
def test_clickbait(title, expected):
    assert is_clickbait(title) is expected


# ── schedule ────────────────────────────────────────────────


def test_parse_schedule():
    assert parse_schedule("09:00=morning, 14:30=midday,21:00=evening") == [
        (9, 0, "morning"), (14, 30, "midday"), (21, 0, "evening")]
    with pytest.raises(ValueError):
        parse_schedule("09:00=lunch")
    with pytest.raises(ValueError):
        parse_schedule("25:00=morning")


# ── LMS parsers ─────────────────────────────────────────────


def test_moodle_parse_events():
    events = [{"id": 77, "name": "Lab 3 is due", "timesort": 1791100800,
               "course": {"fullname": "Machine Learning"}, "url": "https://smart.wsu.ac.kr/mod/assign/view.php?id=5"}]
    [a] = parse_events(events, "smart")
    assert a.id == "smart:77" and a.title == "Lab 3" and a.course == "Machine Learning"
    assert a.due == datetime.fromtimestamp(1791100800, tz=timezone.utc)


def test_canvas_parse_planner():
    raw = 'while(1);' + """[
      {"plannable_type": "assignment", "plannable_id": 10, "context_name": "Deep Learning",
       "html_url": "/courses/1/assignments/10", "plannable": {"title": "HW1", "due_at": "2026-10-06T14:59:00Z"},
       "submissions": {"submitted": true}},
      {"plannable_type": "announcement", "plannable_id": 11, "plannable": {"title": "Hello"}},
      {"plannable_type": "quiz", "plannable_id": 12, "context_name": "DL", "html_url": "/courses/1/quizzes/12",
       "plannable": {"title": "Quiz 1", "due_at": null}, "plannable_date": "2026-10-07T00:00:00Z",
       "submissions": false},
      {"plannable_type": "assessment_request", "plannable_id": 268, "context_name": "DL",
       "html_url": "/courses/1/assignments/10/submissions/5", "submissions": false,
       "plannable": {"title": "HW1", "todo_date": "2026-10-08T05:59:59Z", "workflow_state": "assigned"}},
      {"plannable_type": "assignment", "plannable_id": 13, "planner_override": {"marked_complete": true},
       "plannable": {"title": "Read ch.2", "due_at": "2026-10-09T00:00:00Z"}, "submissions": {"submitted": false}}
    ]"""
    items = parse_planner_items(_loads(raw), "nsmart", "https://nsmart.wsu.ac.kr")
    assert [a.id for a in items] == [
        "nsmart:assignment:10", "nsmart:quiz:12", "nsmart:assessment_request:268", "nsmart:assignment:13"]
    assert items[0].submitted is True and items[0].url == "https://nsmart.wsu.ac.kr/courses/1/assignments/10"
    assert items[1].submitted is None and items[1].due is not None
    assert items[2].title == "Peer review: HW1" and items[2].submitted is False
    assert items[3].submitted is True


# ── storage ─────────────────────────────────────────────────


async def test_storage_roundtrip(tmp_path):
    st = Storage(tmp_path / "t.db")
    await st.init()
    assert await st.filter_unseen("hh", ["a", "b"]) == {"a", "b"}
    await st.mark_seen("hh", ["a"])
    assert await st.filter_unseen("hh", ["a", "b"]) == {"b"}

    assert not await st.has_snapshot("smart")
    await st.save_snapshot("smart", [task("1", NOW), task("2", NOW)])
    await st.save_snapshot("smart", [task("2", NOW)])
    assert set(await st.load_snapshot("smart")) == {"smart:2"}
    assert await st.has_snapshot("smart")

    changes = diff_assignments(await st.load_snapshot("smart"), [task("3", NOW)])
    await st.add_changes(changes)
    rows = await st.unreported_changes()
    assert len(rows) == 2
    await st.mark_changes_reported([i for i, _ in rows])
    assert await st.unreported_changes() == []
