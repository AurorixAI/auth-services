"""Builders of the PAY-F tests (PostgreSQL): a centre with Mon / Wed / Fri groups, prices, payments, pauses, the usual cancellation through the API.

Every test gets its own organisation (``ops_helpers.build_org``); the business clock is ``ops_helpers.clock`` (a test sets the day it needs)."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from src.models.auth import MembershipPause, Organization, StudentGroup, StudentPayment

from tests.ops_helpers import build_org, client, new_group, new_slot, new_student  # noqa: F401
from tests.pg_world import give_group_price

D = date
API = "/api/v1/auth"
MWF = (("lesson-mon", 1), ("lesson-wed", 3), ("lesson-fri", 5))


def slots(valid_from: str = "2026-01-01") -> List[dict]:
    return [new_slot(lid, dow, "10:00", "11:00", valid_from) for lid, dow in MWF]


def set_payments(w, **block) -> None:
    """The ``payments`` block of the centre's settings (the accounting starts 01.09 unless a test says otherwise)."""
    org = w.db.get(Organization, w.org.id)
    org.settings = {**(org.settings or {}), "payments": {"accounting_start_date": "2026-09-01", **block}}
    w.db.commit()


def centre(w, *, groups: int = 1, price: int = 600_000, joined: datetime = datetime(2026, 10, 6, 6, 0), **block):
    """A centre with ``groups`` Mon / Wed / Fri groups (teacher A), one student in each, the price ``price``; returns ``(groups, students, memberships)``."""
    set_payments(w, **block)
    gs, ss, ms = [], [], []
    for i in range(groups):
        group = new_group(w.db, w.org, w.ids, f"G{i}", f"Группа {i + 1}", w.A, slots())
        student = new_student(w.db, w.org, w.ids, f"s{i}", f"Ученик{i}", "Тест", group, joined=joined)
        give_group_price(w.db, group, price, valid_from=D(2026, 1, 1))
        gs.append(group)
        ss.append(student)
    w.db.commit()
    for group, student in zip(gs, ss):
        ms.append(w.db.query(StudentGroup).filter_by(group_id=group.id, student_id=student.id).one())
    return gs, ss, ms


def pay(w, membership, amount: int, paid_on: date) -> StudentPayment:
    row = StudentPayment(org_id=w.org.id, membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id, amount=amount,
                         paid_on=paid_on, created_by=w.owner.id, method="cash", created_at=datetime(paid_on.year, paid_on.month, paid_on.day, 9))
    w.db.add(row)
    w.db.commit()
    return row


def pause(w, membership, first: date, last: date) -> MembershipPause:
    row = MembershipPause(org_id=w.org.id, membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
                          first_day=first, last_day=last, created_by=w.owner.id)
    w.db.add(row)
    w.db.commit()
    return row


def cancel(w, group, lesson_id: str, day: str, kind: Optional[str] = None, who: str = "owner", reason: str = "Преподаватель заболел", expect: int = 200):
    """The usual cancellation of a lesson (the teacher's / admin's dialog): ``kind`` None = the field is not sent (the server's default)."""
    body = {"lessonDate": day, "lessonId": lesson_id, "status": "cancelled", "records": {}, "cancellationReason": reason}
    if kind is not None:
        body["cancellationKind"] = kind
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance", json=body)
    assert r.status_code == expect, r.text
    return r


def rows(w, student) -> List[dict]:
    w.db.expire_all()
    r = client(w, "owner").get(f"{API}/org/students/{student.id}/memberships")
    assert r.status_code == 200, r.text
    return r.json()


def statement(w, membership) -> dict:
    w.db.expire_all()
    r = client(w, "owner").get(f"{API}/org/memberships/{membership.id}/statement")
    assert r.status_code == 200, r.text
    return r.json()


def lesson_id_on(day: date) -> str:
    return dict((i, lid) for lid, i in MWF)[day.isoweekday()]
