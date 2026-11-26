"""Builders of the LESSON-RULES tests (PostgreSQL): the world of the analysis §8.1 (the group «Г», Mon / Wed 10:00-11:30, 9 lessons in November 2026, price 450 000, the shared month, the
recalculation of the centre on from 01.11), the cancellations of the roles, the confirmation, the account of a student.

The world: nine students joined 20.10, each paid 450 000 for November on 01.11 (the account is 0).  A lesson cancelled «по вине центра» deducts 450 000 / 9 = 50 000 from every student
(nine students: 450 000 in all).  Today is Thursday 26.11.2026 12:00 Tashkent; Monday 23.11 (3 days ago) is the lesson the teacher «A» cancels."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Tuple

from src.models.auth import StudentGroup

from tests.ops_helpers import clock, client, grant_money_right, needs_pg, new_group, new_slot, new_student, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, build_org, pay, set_payments, statement
from tests.pg_world import give_group_price

MON, WED = "lesson-mon", "lesson-wed"
PAST_MON = "2026-11-23"            # a Monday, 3 days before today (26.11)


def world(db, clock, *, students: int = 9, price: int = 450_000, recalc: bool = True, money_right: bool = False, salt=None, **block):
    """``(w, group, students, memberships)``; ``money_right``: the owner let the administrator decide the money (#159)."""
    w = build_org(db, salt)
    if money_right:
        grant_money_right(w)
    set_payments(w, **{"accounting_start_date": "2026-11-01", "period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 1, **block})
    clock.set("2026-11-01 09:00")
    group = new_group(db, w.org, w.ids, "GL", "Г", w.A, [new_slot(MON, 1, "10:00", "11:30", "2026-01-01"), new_slot(WED, 3, "10:00", "11:30", "2026-01-01")])
    give_group_price(db, group, price, valid_from=D(2026, 1, 1))
    people = [new_student(db, w.org, w.ids, f"n{i}", f"Ученик{i + 1}", "Тест", group, joined=datetime(2026, 10, 20, 6, 0)) for i in range(students)]
    db.commit()
    members = [db.query(StudentGroup).filter_by(group_id=group.id, student_id=s.id).one() for s in people]
    for m in members:
        pay(w, m, price, D(2026, 11, 1))
    if recalc:
        r = client(w, "owner").put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True})
        assert r.status_code == 200 and r.json()["centre_cancel_recalc"] is True, r.text
    clock.set("2026-11-26 12:00")
    return w, group, people, members


def account(w, membership) -> Tuple[int, int, int]:
    """``(advance, unpaid, recalculated)`` of the statement of one membership - the whole account of a student in three numbers."""
    s = statement(w, membership)["summary"]
    return int(s["advance"]), int(s["unpaid"]), int(s["recalculated"])


def accounts(w, members) -> List[Tuple[int, int, int]]:
    return [account(w, m) for m in members]


def teacher_cancels(w, group, day: str = PAST_MON, lesson: str = MON, who: str = "A", reason: str = "Урока не было", expect: int = 200, **extra):
    """«Урока не было» of a lesson that was never started (the list «без отметки»): ``POST .../attendance/reconcile/cancel``."""
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance/reconcile/cancel", json={"lessonDate": day, "lessonId": lesson, "cancellationReason": reason, **extra})
    assert r.status_code == expect, r.text
    return r


def kind_in_db(w, group, day: str = PAST_MON, lesson: str = MON):
    from src.models.auth import GroupAttendance

    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=group.id, lesson_date=day, lesson_id=lesson).first()
    return None if row is None else (row.status, row.cancellation_kind)


def set_kind(w, group, kind, *, day: str = PAST_MON, lesson: str = MON, who: str = "owner", reason=None, expect: int = 200, dry_run: bool = False, expected="unconfirmed", **extra):
    """``PUT .../attendance/cancellation-kind``: by default the CONFIRMATION of a waiting cancellation (``expectedKind`` = ``unconfirmed``); ``expected=None`` does not send the field."""
    body = {"lessonDate": day, "lessonId": lesson, "cancellationKind": kind, **({"reason": reason} if reason is not None else {}), **({"expectedKind": expected} if expected else {}), **extra}
    r = client(w, who).put(f"{API}/org/groups/{group.id}/attendance/cancellation-kind", params={"dry_run": "true"} if dry_run else None, json=body)
    assert r.status_code == expect, r.text
    return r


def reconcile(w, group, day: str, lesson: str = WED, who: str = "A", expect: int = 200):
    """«Оформить» (``POST .../attendance/reconcile``): the lesson that was never started becomes a row ``in_progress`` with the roster, ``started_at`` empty."""
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance/reconcile", json={"lessonDate": day, "lessonId": lesson})
    assert r.status_code == expect, r.text
    return r


def marks_of(people, mark: str = "absent", first_present: int = 0) -> dict:
    """``{student id: mark}``: every student ``mark``, the first ``first_present`` of them ``present``."""
    return {str(s.id): ("present" if i < first_present else mark) for i, s in enumerate(people)}


def save(w, group, people, day: str, lesson: str = WED, who: str = "A", status: str = "completed", marks=None, expect: int = 200, **extra):
    """``POST .../attendance`` with the version the screen saw (the lesson record is read from the database like the journal does)."""
    from tests.ops_helpers import seen_version

    body = {"lessonDate": day, "lessonId": lesson, "status": status, "records": marks if marks is not None else marks_of(people), **seen_version(w, group, day, lesson), **extra}
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance", json=body)
    assert r.status_code == expect, r.text
    return r


def give_rate(w, percent: str = "50"):
    """The percent of the teacher A for the group lessons (the sheet cannot be saved without it)."""
    from decimal import Decimal

    from src.models.auth import PayrollRate

    w.db.add(PayrollRate(org_id=w.org.id, teacher_id=w.A.id, lesson_type="group", percent=Decimal(percent), valid_from=date(2020, 1, 1), created_by=w.owner.id))
    w.db.commit()


def save_sheet(w, month: str = "2026-11", who: str = "owner", expect: int = 201, **extra):
    """``POST /org/payroll/sheets``: saving the sheet of the teacher A for ``month`` (after the end of the month it closes the month)."""
    if "expected_fingerprint" not in extra:          # the screen saves what it looked at: the fingerprint of the calculation it showed
        shown = client(w, who).post(f"{API}/org/payroll/months/{month}/calculate", json={"teacher_ids": [str(w.A.id)]}).json()["sheets"][0]
        extra = {**extra, "expected_fingerprint": shown["source_fingerprint"]}
    r = client(w, who).post(f"{API}/org/payroll/sheets", json={"month": month, "teacher_id": str(w.A.id), **extra})
    assert r.status_code == expect, r.text
    return r
