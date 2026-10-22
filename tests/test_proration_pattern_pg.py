"""A partial period costs ``price x the student's lessons / the lessons of a FULL month by the schedule`` - ``validFrom`` of a slot never shrinks the denominator
(2nd stage rehearsal: with ``validFrom`` = the creation day of the group August became 1/2 = 250 000 instead of 1/13 = 38 000, +6 255 000 on 20 memberships).
The numerator counts only real lessons (from the join, outside pauses, from ``validFrom``)."""
from datetime import date, datetime

import pytest
from sqlalchemy import text

from src.models.auth import Organization

from src.auth.billing_ledger import round_1000
from tests.ops_helpers import build_org, client, clock, needs_pg, new_group, new_slot, new_student, pg_session  # noqa: F401
from tests.pg_world import give_group_price
from tests.test_ops_backfill_history_pg import migration

pytestmark = needs_pg
API = "/api/v1/auth"


def setup(w, *, accounting_start, price=600_000):
    org = w.db.get(Organization, w.org.id)
    org.settings = {"payments": {"accounting_start_date": accounting_start, "billing_anchor": "fixed_day", "billing_day": 1}}
    w.db.commit()


def debt_of(w, student):
    rows = client(w, "owner").get(f"{API}/org/students/{student.id}/memberships").json()
    return rows[0]["debt"], rows[0]


def weekdays(first_valid_from=None):
    return [new_slot(f"lesson-d{d}", d, "10:00", "11:00", first_valid_from or "2026-01-01") for d in range(1, 6)]


def count_weekdays(first, last):
    from datetime import timedelta

    n, d = 0, first
    while d <= last:
        n += d.isoweekday() <= 5
        d += timedelta(days=1)
    return n


def test_a_group_created_on_27_08_with_a_mon_fri_slot_prorates_august_by_the_full_month(pg_session, clock):
    """Created 27.08, student joined 28.08: August (21 weekdays; his 28.08 and 31.08 = 2) costs round_1000(price x 2 / 21) - the denominator is the whole month,
    not the 3 lessons since the creation day.  The legacy slots get ``validFrom`` from the migration step, the result is the same before and after."""
    clock.set("2026-08-31 18:00")
    w = build_org(pg_session)
    setup(w, accounting_start="2026-08-01")
    group = new_group(w.db, w.org, w.ids, "G827", "Август", w.A, weekdays(), created=datetime(2026, 8, 27, 5, 0))
    group.schedule = [{k: v for k, v in slot.items() if k != "validFrom"} for slot in group.schedule]      # the old schema (reassigned: JSON is not change-tracked)
    student = new_student(w.db, w.org, w.ids, "s28", "Поздний", "Август", group, joined=datetime(2026, 8, 28, 5, 0))
    give_group_price(w.db, group, 600_000, valid_from=date(2026, 1, 1))
    w.db.commit()
    expected = round_1000(600_000 * 2, 21)
    assert count_weekdays(date(2026, 8, 1), date(2026, 8, 31)) == 21 and expected == 57_000
    before, row = debt_of(w, student)
    assert before == expected
    w.db.commit()
    migration()._backfill_schedule(w.db.connection())
    w.db.commit()
    w.db.expire_all()
    assert {s["validFrom"] for s in w.db.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": group.id}).scalar_one()} == {"2026-08-27"}
    assert debt_of(w, student)[0] == expected                         # validFrom = 27.08 does not change the August charge


def test_a_group_created_mid_month_prorates_its_first_month_by_the_full_month_denominator(pg_session, clock):
    """A NEW group (slots valid from its creation day 12.10), a student who joined 14.10: October = price x his 13 lessons / 22 lessons of the full month."""
    clock.set("2026-10-21 14:00")
    w = build_org(pg_session)
    setup(w, accounting_start="2026-10-01")
    group = new_group(w.db, w.org, w.ids, "G1012", "Октябрь", w.A, weekdays("2026-10-12"), created=datetime(2026, 10, 12, 5, 0))
    student = new_student(w.db, w.org, w.ids, "s14", "Ученик", "Октябрь", group, joined=datetime(2026, 10, 14, 5, 0))
    give_group_price(w.db, group, 600_000, valid_from=date(2026, 1, 1))
    w.db.commit()
    mine = count_weekdays(date(2026, 10, 14), date(2026, 10, 31))
    whole = count_weekdays(date(2026, 10, 1), date(2026, 10, 31))
    assert (mine, whole) == (13, 22)
    debt, row = debt_of(w, student)
    assert debt == round_1000(600_000 * mine, whole)                     # not mine / (lessons since 12.10 = 15)
    assert debt != round_1000(600_000 * mine, count_weekdays(date(2026, 10, 12), date(2026, 10, 31)))


def test_the_charge_does_not_depend_on_the_valid_from_of_the_slots(pg_session, clock):
    """The same membership, the same pattern: validFrom 2000-01-01 / the creation day / the join day give the same debt (the numerator is the student's, the
    denominator is the pattern)."""
    clock.set("2026-10-21 14:00")
    w = build_org(pg_session)
    setup(w, accounting_start="2026-10-01")
    group = new_group(w.db, w.org, w.ids, "GV", "Валид", w.A, weekdays("2026-10-14"), created=datetime(2026, 10, 12, 5, 0))
    student = new_student(w.db, w.org, w.ids, "sv", "Ученик", "Валид", group, joined=datetime(2026, 10, 14, 5, 0))
    give_group_price(w.db, group, 600_000, valid_from=date(2026, 1, 1))
    w.db.commit()
    seen = set()
    for valid_from in ("2000-01-01", "2026-10-12", "2026-10-14"):
        group.schedule = [dict(s, validFrom=valid_from) for s in group.schedule]
        w.db.commit()
        w.db.expire_all()
        seen.add(debt_of(w, student)[0])
    assert len(seen) == 1
