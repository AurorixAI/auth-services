#!/usr/bin/env python3
"""Retry the release of the exams of archived groups whose release exam-service has not confirmed (``groups.exam_release_pending``).

Idempotent; run it from cron / by hand.  Prints how many groups were cleared and how many are still pending.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from sqlalchemy.orm import Session

    from src.auth.group_lifecycle import GroupLifecycle
    from src.auth.models import Group
    from src.auth.service import AuthService
    from src.core.database import engine

    with Session(engine) as db:
        cleared = GroupLifecycle(AuthService(db)).retry_pending_releases()
        left = db.query(Group).filter(Group.exam_release_pending.is_(True)).count()
    print(f"cleared: {cleared}, still pending: {left}")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
