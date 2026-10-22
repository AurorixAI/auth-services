#!/usr/bin/env python3
"""INV-O2-1: list the ACTIVE groups whose teacher is not an active member any more (legacy data from before LOGIC-B O2: the old delete left the
groups with a removed teacher).  Read only.

    python scripts/report_inactive_group_teachers.py        # prints one line per group, exit 1 when there is any
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def find_violations(db) -> List[Dict[str, Any]]:
    from src.models.auth import Group, OrganizationMember, User

    rows = (
        db.query(Group, User, OrganizationMember)
        .join(User, User.id == Group.teacher_id)
        .outerjoin(OrganizationMember, (OrganizationMember.org_id == Group.org_id) & (OrganizationMember.user_id == Group.teacher_id))
        .filter(Group.status == "active")
        .all()
    )
    out = []
    for group, user, member in rows:
        if member is None or not member.is_active or user.is_active is False:
            out.append({"org_id": str(group.org_id), "group_id": str(group.id), "group": group.name, "teacher_id": str(user.id),
                        "teacher": user.email, "member": "none" if member is None else ("active" if member.is_active else "removed")})
    return out


def main() -> int:
    from sqlalchemy.orm import Session

    from src.core.database import engine  # the application engine (DATABASE_URL)

    with Session(engine) as db:
        found = find_violations(db)
    for item in found:
        print(f"{item['org_id']} group «{item['group']}» ({item['group_id']}): teacher {item['teacher']} is {item['member']}")
    print(f"{len(found)} group(s) with an inactive teacher")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
