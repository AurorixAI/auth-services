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


def world(db, clock, *, students: int = 9, price: int = 450_000, recalc: bool = True, money_right: bool = False, **block):
    """``(w, group, students, memberships)``; ``money_right``: the owner let the administrator decide the money (#159)."""
    w = build_org(db)
    if money_right:
        grant_money_right(w)
    set_payments(w, accounting_start_date="2026-11-01", period_mode="calendar", billing_anchor="fixed_day", billing_day=1, **block)
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


def set_kind(w, group, kind, *, day: str = PAST_MON, lesson: str = MON, who: str = "owner", reason=None, expect: int = 200, **extra):
    body = {"lessonDate": day, "lessonId": lesson, "cancellationKind": kind, **({"reason": reason} if reason is not None else {}), **extra}
    r = client(w, who).put(f"{API}/org/groups/{group.id}/attendance/cancellation-kind", json=body)
    assert r.status_code == expect, r.text
    return r
