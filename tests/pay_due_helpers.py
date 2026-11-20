"""PAY-DUE П1: the worlds and the handles of the tests of the term of payment (PostgreSQL).  Everything goes the way a person does it: the clock of the test is moved explicitly, the term is set by the
owner's PUT, a payment is the route of the product; the schedule of a group is Mon / Wed / Fri (or Wednesday only): never «every day» (LESSONS 137)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from src.models.auth import StudentGroup

from tests.ops_helpers import build_org, client, clock, grant_money_right, needs_pg, new_group, new_slot, new_student, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, set_payments, slots
from tests.pg_world import give_group_price

WEDNESDAY = [new_slot("lesson-wed", 3, "10:00", "11:00", "2026-01-01")]


def world(pg_session, clock, today: str = "2026-10-12", *, joins: Sequence[date] = (D(2026, 10, 1), D(2026, 10, 6), D(2026, 10, 9)), price: int = 600_000,
          schedule: Optional[List[dict]] = None, **block):
    """A centre, ONE group (Mon / Wed / Fri unless ``schedule``), a student per date of ``joins``; ``block`` = the payments settings (mode).  The clock stands at ``today`` 09:00 Tashkent."""
    clock.set(f"{today} 09:00")
    w = build_org(pg_session)
    grant_money_right(w)
    set_payments(w, **block)
    group = new_group(w.db, w.org, w.ids, "G0", "Группа", w.A, schedule or slots())
    give_group_price(w.db, group, price, valid_from=D(2026, 1, 1))
    students = [new_student(w.db, w.org, w.ids, f"s{i}", f"Ученик{i}", "Тест", group, joined=datetime(d.year, d.month, d.day, 6, 0)) for i, d in enumerate(joins)]
    w.db.commit()
    memberships = [w.db.query(StudentGroup).filter_by(group_id=group.id, student_id=s.id).one() for s in students]
    return w, group, students, memberships


def put_settings(w, who: str = "owner", expect: int = 200, **body) -> dict:
    r = client(w, who).put(f"{API}/org/settings/payments", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def dry_run(w, **body) -> dict:
    r = client(w, "owner").put(f"{API}/org/settings/payments", params={"dry_run": "true"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def pay(w, membership, amount: int, day: date, expect: int = 201) -> dict:
    r = client(w, "owner").post(f"{API}/org/memberships/{membership.id}/payments", json={"amount": amount, "paid_on": day.isoformat(), "method": "cash"})
    assert r.status_code == expect, r.text
    return r.json()


def summary(w, who: str = "owner", expect: int = 200) -> dict:
    r = client(w, who).get(f"{API}/org/payments/summary")
    assert r.status_code == expect, r.text
    return r.json()


def status_page(w, bucket: str, who: str = "owner", **params) -> dict:
    w.db.expire_all()
    r = client(w, who).get(f"{API}/org/payments/status", params={"bucket": bucket, "limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()
