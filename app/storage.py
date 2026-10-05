"""SQLite state: dedupe of seen items, LMS snapshots, detected changes, small key-value store."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Assignment, AssignmentChange

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lms_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ── seen items (jobs / news) ────────────────────────────
    async def filter_unseen(self, source: str, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        async with aiosqlite.connect(self.path) as db:
            marks = ",".join("?" * len(ids))
            cur = await db.execute(
                f"SELECT item_id FROM seen WHERE source = ? AND item_id IN ({marks})", (source, *ids)
            )
            seen = {row[0] for row in await cur.fetchall()}
        return set(ids) - seen

    async def mark_seen(self, source: str, ids: list[str]) -> None:
        if not ids:
            return
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO seen (source, item_id, first_seen) VALUES (?, ?, ?)",
                [(source, i, now) for i in ids],
            )
            await db.commit()

    # ── LMS snapshot ────────────────────────────────────────
    async def load_snapshot(self, source: str) -> dict[str, Assignment]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT data FROM assignments WHERE source = ?", (source,))
            rows = await cur.fetchall()
        items = (Assignment.model_validate_json(r[0]) for r in rows)
        return {a.id: a for a in items}

    async def has_snapshot(self, source: str) -> bool:
        return await self.get_kv(f"snapshot_at:{source}") is not None

    async def all_assignments(self) -> list[Assignment]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT data FROM assignments")
            rows = await cur.fetchall()
        return [Assignment.model_validate_json(r[0]) for r in rows]

    async def save_snapshot(self, source: str, assignments: list[Assignment]) -> None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            ids = [a.id for a in assignments]
            if ids:
                marks = ",".join("?" * len(ids))
                await db.execute(
                    f"DELETE FROM assignments WHERE source = ? AND id NOT IN ({marks})", (source, *ids)
                )
            else:
                await db.execute("DELETE FROM assignments WHERE source = ?", (source,))
            await db.executemany(
                """INSERT INTO assignments (id, source, data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET data = excluded.data, last_seen = excluded.last_seen""",
                [(a.id, source, a.model_dump_json(), now, now) for a in assignments],
            )
            await db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"snapshot_at:{source}", now),
            )
            await db.commit()

    # ── LMS changes ─────────────────────────────────────────
    async def add_changes(self, changes: list[AssignmentChange]) -> None:
        if not changes:
            return
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO lms_changes (data, detected_at) VALUES (?, ?)",
                [(c.model_dump_json(), now) for c in changes],
            )
            await db.commit()

    async def unreported_changes(self) -> list[tuple[int, AssignmentChange]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT id, data FROM lms_changes WHERE reported = 0 ORDER BY id")
            rows = await cur.fetchall()
        return [(r[0], AssignmentChange.model_validate_json(r[1])) for r in rows]

    async def mark_changes_reported(self, ids: list[int]) -> None:
        if not ids:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany("UPDATE lms_changes SET reported = 1 WHERE id = ?", [(i,) for i in ids])
            await db.commit()

    # ── key-value ───────────────────────────────────────────
    async def get_kv(self, key: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def delete_kv(self, key: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM kv WHERE key = ?", (key,))
            await db.commit()
