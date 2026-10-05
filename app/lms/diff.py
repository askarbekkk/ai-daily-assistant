"""Pure diff between the stored LMS snapshot and freshly scraped assignments."""
from __future__ import annotations

from datetime import datetime

from ..models import Assignment, AssignmentChange


def _same_due(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs((a - b).total_seconds()) < 60


def diff_assignments(old: dict[str, Assignment], new: list[Assignment]) -> list[AssignmentChange]:
    """Return only meaningful changes: new tasks, moved deadlines, removed tasks.

    Title/description edits are ignored on purpose to keep the digest quiet.
    """
    changes: list[AssignmentChange] = []
    new_by_id = {a.id: a for a in new}

    for a in new:
        prev = old.get(a.id)
        if prev is None:
            changes.append(AssignmentChange(kind="new", assignment=a))
        elif not _same_due(prev.due, a.due):
            changes.append(AssignmentChange(kind="due_changed", assignment=a, old_due=prev.due))

    for old_id, prev in old.items():
        if old_id not in new_by_id:
            changes.append(AssignmentChange(kind="removed", assignment=prev))

    return changes
