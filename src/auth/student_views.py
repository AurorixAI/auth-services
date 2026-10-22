"""Derived student fields (LOGIC-B O15): "in a group" means a live membership - an active membership in a group that is not archived."""
from __future__ import annotations

from typing import Any, Iterable, Set
from uuid import UUID

from sqlalchemy import exists, func

from .models import Group, Student, StudentGroup


def live_membership_condition(org_id: Any):
    """Correlated EXISTS for a query on ``Student``: the student has an active membership in a live group of the centre."""
    return (
        exists()
        .where(StudentGroup.student_id == Student.id)
        .where(StudentGroup.is_active.is_(True))
        .where(StudentGroup.group_id == Group.id)
        .where(Group.org_id == org_id)
        .where(Group.status == "active")
    )


def students_with_live_group(db, org_id: Any, student_ids: Iterable[Any]) -> Set[UUID]:
    ids = [i for i in student_ids if i is not None]
    if not ids:
        return set()
    rows = (
        db.query(StudentGroup.student_id)
        .join(Group, Group.id == StudentGroup.group_id)
        .filter(
            StudentGroup.student_id.in_(ids),
            StudentGroup.is_active.is_(True),
            Group.org_id == org_id,
            Group.status == "active",
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def count_no_group_students(db, org_id: Any) -> int:
    """Students whose status is "active" but who sit in no live group (the "Без группы" of the dashboard)."""
    return (
        db.query(func.count(Student.id))
        .filter(Student.org_id == org_id, Student.status == "active", ~live_membership_condition(org_id))
        .scalar()
        or 0
    )
