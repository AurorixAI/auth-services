"""Money-1 / Г: holes of the acceptance review MONEY1_G1_REVIEW (N1…N9) — each test carries the numbers of its scenario (PostgreSQL)."""
from datetime import date

import pytest

from src.models.auth import AttendanceExcuse

from tests.money1_helpers import hold_lesson, now_4_nov  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1_attendance_pg import post_save

pytestmark = needs_pg


# ---- Г-1 (N1): the text of a decided excuse does not move the decision --------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_g1_the_text_of_a_decided_excuse_keeps_the_decision_for_everybody(world, now_4_nov, decision):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1 = world.students[0]
    grant_money_right(world)
    assert post_save(world, world.owner, day, lid, ["excused", "present", "present"], correctionReason="Болел", excuseReasons={str(s1.id): "Болел, написал"}).status_code == 200
    excuse = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
    excuse.status, excuse.reviewed_by, excuse.review_note = decision, world.owner.id, "Решено"
    world.db.commit()
    grant_money_right(world, False)
    for who in (world.teacher, world.admin):                       # no right to the money: the text is saved, the decision (pack −1 lesson, teacher +20 000) stays
        r = post_save(world, who, day, lid, ["excused", "present", "present"], excuseReasons={str(s1.id): f"Другой текст {who.id}"})
        assert r.status_code == 200, r.text
        world.db.expire_all()
        row = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
        assert (row.status, row.reviewed_by, row.review_note) == (decision, world.owner.id, "Решено") and row.reason == f"Другой текст {who.id}"


# ---- Г-2 (N2): the first price of a group begins inside the first period of a student who was added before it --------------------------------------------

from tests.money1b_helpers import account, charge_of, set_schedule, slot  # noqa: E402
from tests.pg_world import give_group_price  # noqa: E402
from tests.test_pay2_api_pg import fixed_today, membership, settings_block, stay  # noqa: E402,F401  (fixtures)
from tests.test_money1b_price_pg import everybody_from  # noqa: E402

D = date


def test_g2_the_first_price_from_the_10th_prices_the_period_of_the_student_added_on_the_5th(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))      # set on 01.11 «с 10.11»; the student was added on 05.11
    everybody_from(w, D(2026, 11, 5))
    led = account(w, membership(w).id)
    assert (led.charged, led.debt) == (500_000, 500_000) and charge_of(led, D(2026, 11, 5)) == 500_000      # e7c44aa: 0 and 0 - the first month for free


def test_g2_a_period_that_ended_before_the_first_price_is_still_free_and_the_next_one_is_charged(world, fixed_today):
    w = world
    fixed_today(D(2026, 12, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 10, 1))
    led = account(w, membership(w).id)
    assert charge_of(led, D(2026, 10, 1)) == 0 and charge_of(led, D(2026, 11, 1)) == 500_000       # a month with no price on any day stays unpriced


def test_g2_the_shared_month_cell_of_the_student_added_before_the_first_price_is_priced(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    set_schedule(w, [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 11, 5))
    assert charge_of(account(w, membership(w).id), D(2026, 11, 1)) == 254_000       # the same 11 of 13 lessons x 300 000 as when the price is typed «с сегодня»


