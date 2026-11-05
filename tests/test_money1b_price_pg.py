"""Money-1 pack B2 / B3 (PostgreSQL): the FIRST price of a group and the window of a price change.

B2 (ДЫРА-04 / B1 of the devil): the first price «с сегодня» used to leave the RUNNING period without a price (a period with no price on its first day is not charged) - a free month, and the mistake
could not be fixed (PRICE_BACKDATE_WITH_PAYMENTS).  Now the first price acts from the start of the period that runs for each student; the price can be given when the group is created.
B3 (ДЫРА-01): the window reads the account on the day the change REACHES the students and lists every student who moves, in sums and in lessons."""
from datetime import date

import pytest

from src.models.auth import GroupAudit, GroupPrice

from tests.money1b_helpers import account, charge_of, hold_lessons, set_schedule, slot
from tests.pg_world import give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def everybody_from(w, day):
    for student in w.students:
        stay(w, membership(w, student), day)


def put_price(w, amount, *, valid_from=None, dry_run=False, expect=200):
    body = {"amount": amount, **({"valid_from": valid_from.isoformat()} if valid_from else {})}
    r = owner(w).put(f"{API}/org/groups/{w.group.id}/price", params={"dry_run": 1} if dry_run else {}, json=body)
    assert r.status_code == expect, r.text
    return r.json()


# ---- B2: the first price ---------------------------------------------------------------------------------------------------------------------------


def test_b2_n1_the_first_price_from_today_reaches_the_running_period_of_a_student_who_has_paid(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    everybody_from(w, D(2026, 11, 5))
    m = membership(w)
    seed_payment(w, m, 300_000, D(2026, 11, 6))                         # money lies as an advance: the group has no price yet
    before = account(w, m.id)
    assert before.charged == 0 and before.advance == 300_000            # the old picture: the running month 05.11-04.12 is free
    dry = put_price(w, 300_000, dry_run=True)
    first = dry["impact"]["first_price"]
    assert (first["requested_valid_from"], first["valid_from"], first["moved"]) == ("2026-11-15", "2026-11-05", True)
    assert "с 05.11.2026" in first["text"] and "идущего периода" in first["text"]
    saved = put_price(w, 300_000)                                       # d2: before - PRICE_BACKDATE_WITH_PAYMENTS when corrected later, and «оплачено до 04.01.2027» for 300 000
    assert saved["current"]["valid_from"] == "2026-11-05"
    after = account(w, m.id)
    assert after.charged == 300_000 and after.debt == 0 and after.advance == 0 and after.covered_through == D(2026, 12, 4)
    row = w.db.query(GroupAudit).filter_by(group_id=w.group.id, action="group_price_set").one()
    assert row.snapshot["first_price"] is True and row.snapshot["requested_valid_from"] == "2026-11-15"


def test_b2_n3_the_shared_month_has_its_cell_priced_from_the_first_day_of_the_student(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    set_schedule(w, [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    everybody_from(w, D(2026, 11, 5))
    m = membership(w)
    assert account(w, m.id).charged == 0                                # N3: the whole of November is not charged
    put_price(w, 300_000)
    assert charge_of(account(w, m.id), D(2026, 11, 1)) == 254_000       # 11 of the 13 lessons of November (from Fri 06.11) x 300 000


def test_b2_n4_a_package_in_progress_is_priced_from_its_first_lesson(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, period_mode="lessons", lessons_per_payment=12, accounting_start_date="2026-09-01", billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    everybody_from(w, D(2026, 11, 2))
    m = membership(w)
    hold_lessons(w, w.students[0], [D(2026, 11, 2), D(2026, 11, 4), D(2026, 11, 6)])        # three lessons of the first package before the price
    saved = put_price(w, 600_000)
    assert saved["current"]["valid_from"] == "2026-11-02"
    led = account(w, m.id)
    assert led.charged == 150_000 and led.debt == 150_000              # 3 x 50 000; before: the three lessons were free and the package began at the first priced lesson


def test_b2_a_price_that_is_not_the_first_acts_from_its_date_as_before(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    saved = put_price(w, 350_000, valid_from=D(2026, 12, 1))
    assert saved["current"]["valid_from"] == "2026-01-01" and "first_price" not in {k for k, v in saved["impact"].items() if v}


def test_b2_a_first_price_in_the_future_is_kept_and_the_window_says_the_running_period_stays_free(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    everybody_from(w, D(2026, 11, 5))
    dry = put_price(w, 300_000, valid_from=D(2026, 12, 1), dry_run=True)
    first = dry["impact"]["first_price"]
    assert first["moved"] is False and first["students_without_price"] == 3 and "останется без цены" in first["text"]


def test_b2_the_price_at_the_creation_of_the_group_acts_from_the_beginning(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    r = owner(w).post(f"{API}/org/groups", json={"name": "Алгебра 9", "price": 450_000})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    (row,) = w.db.query(GroupPrice).filter_by(group_id=gid).all()
    assert row.amount == 450_000 and row.valid_from == D(2000, 1, 1)    # «с самого начала»: a student may start before today
    assert w.db.query(GroupAudit).filter_by(group_id=gid, action="group_price_set").count() == 1


def test_b2_a_group_created_without_a_price_has_none_and_a_negative_price_is_refused(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 15))
    r = owner(w).post(f"{API}/org/groups", json={"name": "Без цены"})
    assert r.status_code == 201 and w.db.query(GroupPrice).filter_by(group_id=r.json()["id"]).count() == 0
    assert owner(w).post(f"{API}/org/groups", json={"name": "Минус", "price": -1}).status_code == 422
