#!/usr/bin/env python3
"""Retry the release of the exams that exam-service has not confirmed (idempotent; the same logic the background task of auth runs every 15 minutes).

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

    from src.auth import exam_release_worker
    from src.core.database import engine

    with Session(engine) as db:
        cleared, groups_left, lessons_left = exam_release_worker.run_sweep(db)
    print(f"groups cleared: {cleared}, groups still pending: {groups_left}, lessons still pending: {lessons_left}")
    return 1 if groups_left or lessons_left else 0


if __name__ == "__main__":
    raise SystemExit(main())
