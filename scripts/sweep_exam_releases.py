#!/usr/bin/env python3
"""Retry the release of the exams that exam-service has not confirmed (idempotent; by hand - there is no cron).

* archived groups with ``groups.exam_release_pending``;
* CANCELLED lessons with ``group_attendance.exam_release_pending`` (Wave B P4: a cancellation commits first, exam-service is asked afterwards).

Prints how many are still pending; exit code 1 when something is.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from sqlalchemy.orm import Session

    from src.auth import exam_release
    from src.auth.group_lifecycle import GroupLifecycle
    from src.auth.models import Group, GroupAttendance
    from src.auth.service import AuthService
    from src.core.database import engine

    with Session(engine) as db:
        svc = AuthService(db)
        cleared = GroupLifecycle(svc).retry_pending_releases()
        for (org_id,) in db.query(GroupAttendance.org_id).filter(GroupAttendance.exam_release_pending.is_(True)).distinct().all():
            exam_release.release_pending(svc, org_id, limit=10_000)
        groups_left = db.query(Group).filter(Group.exam_release_pending.is_(True)).count()
        lessons_left = db.query(GroupAttendance).filter(GroupAttendance.exam_release_pending.is_(True)).count()
    print(f"groups cleared: {cleared}, groups still pending: {groups_left}, lessons still pending: {lessons_left}")
    return 1 if groups_left or lessons_left else 0


if __name__ == "__main__":
    raise SystemExit(main())
