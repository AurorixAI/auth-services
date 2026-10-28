#!/usr/bin/env python3
"""W1 round 2 (#156, «уроки ведёт только преподаватель»): the ONE-TIME cleanup of the test data of stage / prod (every centre there is a test one; LESSONS 49: no migration of data).

    python scripts/w1_only_teachers_cleanup.py            # dry run (the default): per centre, only COUNTERS - no names, no e-mails
    python scripts/w1_only_teachers_cleanup.py --apply    # does it, ONE transaction for all the centres

What it does for every member of the centres whose role is owner / admin (the people who must not lead lessons any more):
  1. takes off his FUTURE substitutions (the same function as a dismissal / a change of the role: reason «Роль изменена», trigger ``role_changed``; the substitutions decided from
     a request are reopened as new requests, the lessons that already began keep their teacher);
  2. sets ``teacher_id = NULL`` in the groups of ANY status that he leads (a ``group_audit`` row ``teacher_changed``, via ``cleanup_only_teachers``);
  3. the waiting requests of colleagues that suggested him get the event ``suggestion_unavailable`` (the same function) and stay waiting.
It does NOT touch the rates, the past lessons, the sheets, the payouts and the existing rows of any journal.

The run is the same code in both modes; a dry run rolls the transaction back, so its counters are exactly what ``--apply`` does.  The operator runs it: on stage after the deploy, on prod
after the owner's «да» and a backup of the database.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VIA = "cleanup_only_teachers"


def cleanup(db) -> List[Dict[str, Any]]:
    """Does the cleanup in the open transaction of ``db`` (the caller commits or rolls back) and returns one counters row per centre that had something to do."""
    from src.auth.service import AuthService
    from src.auth.staff_lifecycle import StaffLifecycle
    from src.models.auth import Group, GroupAudit, LessonSubstitutionEvent as Event, Organization, OrganizationMember

    svc = AuthService(db)
    life = StaffLifecycle(svc)
    out: List[Dict[str, Any]] = []
    for org in db.query(Organization).order_by(Organization.id).all():
        members = db.query(OrganizationMember).filter(OrganizationMember.org_id == org.id, OrganizationMember.org_role.in_(("owner", "admin"))).all()
        if not members:
            continue
        managers = [m.user_id for m in members]
        actor = next((m.user_id for m in members if m.org_role == "owner"), managers[0])          # who the cancellations are recorded under: the owner of the centre
        row = {"org_id": str(org.id), "org_name": org.name, "released_substitutions": 0, "closed_requests": 0, "reopened_requests": 0,
               "suggestion_events": 0, "groups_unassigned": 0, "groups_archived_unassigned": 0}
        for uid in managers:
            events_before = db.query(Event).filter(Event.org_id == org.id, Event.action == "suggestion_unavailable").count()
            released, closed, reopened = life._release_work_of(org.id, uid, actor, role_changed=True)
            db.flush()
            row["suggestion_events"] += db.query(Event).filter(Event.org_id == org.id, Event.action == "suggestion_unavailable").count() - events_before
            row["released_substitutions"] += released
            row["closed_requests"] += closed
            row["reopened_requests"] += reopened
        for group in db.query(Group).filter(Group.org_id == org.id, Group.teacher_id.in_(managers)).order_by(Group.id).with_for_update().all():
            old = group.teacher_id
            group.teacher_id = None
            db.add(GroupAudit(org_id=org.id, group_id=group.id, actor_id=actor, actor_role="owner", action="teacher_changed", assigned_teacher_id=None,
                              snapshot={"teacher_id": [str(old), None], "via": VIA}))
            row["groups_unassigned" if group.status == "active" else "groups_archived_unassigned"] += 1
        db.flush()
        if any(v for k, v in row.items() if k not in ("org_id", "org_name")):
            out.append(row)
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="do it (one transaction); without it a dry run that changes nothing")
    args = parser.parse_args(argv)

    from sqlalchemy.orm import Session

    from src.core.database import engine  # the application engine (DATABASE_URL)

    with Session(engine) as db:
        try:
            rows = cleanup(db)
            if args.apply:
                db.commit()
            else:
                db.rollback()
        except BaseException:
            db.rollback()
            raise
    mode = "APPLIED" if args.apply else "DRY RUN (nothing changed)"
    for r in rows:
        print(f"centre {r['org_id']} «{r['org_name']}»: substitutions taken off {r['released_substitutions']}, requests closed {r['closed_requests']}, requests reopened {r['reopened_requests']}, "
              f"suggestions marked {r['suggestion_events']}, groups left without a teacher {r['groups_unassigned']} (+{r['groups_archived_unassigned']} archived)")
    print(f"{mode}: {len(rows)} centre(s) with something to clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
