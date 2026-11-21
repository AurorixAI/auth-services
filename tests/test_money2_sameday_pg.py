"""Money-2 stage fix С1: a stay that was opened and closed on ONE business day charges nothing (PostgreSQL).

Rule: a transfer there and back, a move on through a third group, a student written down and taken out the same day leave no charge in the group they only passed through, and the money that
came in with the transfer leaves it whole.  A stay that is still open, and a return on the NEXT day, are charged as before.

Calendar of the tests: Tuesday 17.11.2026.  A = Mon/Wed/Fri (no lesson today), B = Tue/Thu/Sat (a lesson today), C = Tue/Fri.
November 2026 lessons: A = Mon 2,9,16,23,30 + Wed 4,11,18,25 + Fri 6,13,20,27 = 13; B = Tue 3,10,17,24 + Thu 5,12,19,26 + Sat 7,14,21,28 = 12.
B charges 450 000 x 1/12 = 37 500 -> 38 000 (whole thousands) for today's lesson: that is the ghost charge of the stage walk (D1)."""
from datetime import date, datetime

import pytest

from src.auth import money_reconcile as mr
from src.models.auth import StudentGroup

from tests.money1b_helpers import account, add, hold_lessons, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

TUE = date(2026, 11, 17)
MWF = [slot(n, d, "2026-01-01") for n, d in (("mon", 1), ("wed", 3), ("fri", 5))]
TTS = [slot(n, d, "2026-01-01") for n, d in (("tue", 2), ("thu", 4), ("sat", 6))]
TF = [slot(n, d, "2026-01-01") for n, d in (("tue", 2), ("fri", 5))]


def three(w, fixed_today, mode="shared"):
    """A (600 000), B (450 000), C (500 000); ``mode``: shared = «Общий месяц» (calendar, fixed day 1), personal = «Личный период» (calendar, payment date), lessons = a package of 12."""
    fixed_today(TUE)
    block = {"accounting_start_date": "2026-10-01"}
    block.update({"shared": dict(billing_anchor="fixed_day", billing_day=1), "personal": dict(billing_anchor="payment_date"), "lessons": dict(period_mode="lessons", lessons_per_payment=12)}[mode])
    settings_block(w, **block)
    ensure_billing(w)
    give_group_price(w.db, w.group, 600_000)
    b, c = make_group(w.db, w.org, w.teacher, "Бета"), make_group(w.db, w.org, w.teacher, "Гамма")
    give_group_price(w.db, b, 450_000)
    give_group_price(w.db, c, 500_000)
    for group, slots in ((w.group, MWF), (b, TTS), (c, TF)):
        set_schedule(w, slots, group)
    return w.group, b, c


def numbers(w, student, group):
    led = account(w, membership(w, student, group).id)
    return (led.charged, led.paid, led.advance, led.debt, led.covered_through, led.transferred_in - led.transferred_out)


def settle(w, student, a, joined=date(2026, 11, 2), paid=600_000):
    stay(w, membership(w, student, a), joined)
    pay(w, student, a, amount=paid)


@pytest.mark.parametrize("mode", ["shared", "personal"])
def test_a_transfer_there_and_back_the_same_day_leaves_nothing_in_b_and_gives_a_its_numbers_back(world, fixed_today, mode):
    # shared month: A charged 600 000 for all 13 lessons, paid 600 000; B has a lesson today (450 000 x 1/12 = 37 500 -> 38 000 used to stay in B and 562 000 came back).
    a, b, c = three(world, fixed_today, mode)
    s0 = world.students[0]
    settle(world, s0, a)
    before = numbers(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    add(world, s0, group=a, mode="transfer")
    mb = membership(world, s0, b)
    nb = numbers(world, s0, b)
    assert nb[0] == 0 and nb[3] == 0 and nb[5] == 0, nb               # nothing charged, no debt, all that came in went out
    assert numbers(world, s0, a) == before


def test_the_same_with_a_pause_in_a_that_travels_there_and_back(world, fixed_today):
    from tests.test_money2_transfer_a_pg import pause

    a, b, c = three(world, fixed_today)
    s0 = world.students[0]
    settle(world, s0, a)
    pause(world, a, s0, date(2026, 11, 25), date(2026, 11, 28))
    before = numbers(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    add(world, s0, group=a, mode="transfer")
    nb = numbers(world, s0, b)
    assert nb[0] == 0 and nb[3] == 0 and nb[5] == 0, nb
    assert numbers(world, s0, a) == before


def test_a_to_b_to_c_the_same_day_b_charges_nothing_and_the_money_goes_whole_to_c(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = world.students[0]
    settle(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    carried_to_b = account(world, membership(world, s0, b).id).transferred_in
    add(world, s0, group=c, mode="transfer")
    nb = numbers(world, s0, b)
    assert nb[0] == 0 and nb[3] == 0 and nb[5] == 0, nb
    assert numbers(world, s0, c)[5] == carried_to_b > 0


def test_written_down_and_taken_out_the_same_day_into_a_group_with_a_lesson_today_charges_nothing_and_there_is_nothing_to_forgive(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = world.students[1]
    add(world, s, group=b, mode="add")
    assert numbers(world, s, b)[0] > 0                                           # still in the group: today's lesson is charged as before (450 000 x 1/12)
    assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    n = numbers(world, s, b)
    assert n[0] == 0 and n[3] == 0, n
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, b).id}/writeoffs", json={"all": True, "reason": "Ошибка"})
    assert r.status_code in (400, 409, 422), r.text
    result = mr.run(world.db, apply=True, only=[world.org.id])
    assert result["failed"] == [] and [i for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-1"] == []


def test_a_student_written_down_today_and_not_taken_out_is_charged_as_before(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = world.students[1]
    add(world, s, group=b, mode="add")
    n = numbers(world, s, b)
    assert n[0] == 225_000 and n[3] == 225_000, n      # open: his lessons from 17.11 to the end of the month = Tue 17,24 + Thu 19,26 + Sat 21,28 = 6 of 12 = 225 000


def test_a_transfer_back_the_next_day_is_ordinary_b_charges_its_own_day(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = world.students[0]
    settle(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    fixed_today(date(2026, 11, 18))
    add(world, s0, group=a, mode="transfer")
    assert numbers(world, s0, b)[0] == 38_000                                    # Tuesday 17th: his one day in B is charged


def test_a_package_centre_a_one_day_stay_without_a_mark_charges_nothing_and_with_a_mark_keeps_the_lesson(world, fixed_today):
    # a package spends a lesson the student was marked at: no mark = no deduction (as before), a mark = the lesson is his and stays spent
    a, b, c = three(world, fixed_today, "lessons")
    s0, s1 = world.students[0], world.students[1]
    for s in (s0, s1):
        add(world, s, group=b, mode="add")
    hold_lessons(world, s1, [TUE], group=b)
    for s in (s0, s1):
        assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    without, marked = account(world, membership(world, s0, b).id), account(world, membership(world, s1, b).id)
    assert without.lessons.used == 0 and without.charged == 0
    assert marked.lessons.used == 1, marked.lessons


def test_an_old_forgiveness_on_a_one_day_stay_is_an_honest_finding_of_the_night_and_not_an_advance(world, fixed_today):
    # data of the stage: the stay was charged 38 000 under the old rule and the owner forgave 38 000; now the stay charges 0: the forgiveness has no debt to cover -> I-1 «writeoff» (honest finding of old data)
    from src.models.auth import StudentWriteoff

    a, b, c = three(world, fixed_today)
    s = world.students[1]
    add(world, s, group=b, mode="add")
    assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    m = membership(world, s, b)
    world.db.add(StudentWriteoff(org_id=world.org.id, membership_id=m.id, student_id=s.id, group_id=b.id, amount=38_000, reason="Старое прощение", effective_on=TUE, created_by=world.owner.id))
    world.db.commit()
    led = account(world, m.id)
    assert (led.charged, led.advance, led.debt, led.written_off, led.written_off_granted) == (0, 0, 0, 0, 38_000)      # PAY-TAILS: no charge, no debt, NO advance (a forgiveness is not money); nothing of the 38 000 was used
    result = mr.run(world.db, apply=True, only=[world.org.id])
    # PAY-TAILS А7.5: the rows equal what the ledger took in (38 000) and the USED part is 0 <= 0, but the sum ENTERED is above everything the begun periods would cost (0): the honest finding of old data
    # returns (the old meaning of this test), while the advance stays 0 - the forgiveness is not money
    assert [i["key"] for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-1"] == [f"membership:{m.id}:writeoff"]


# ---- С2: the forecast «хватит до» of a package counts the lessons from «Оплата с», not from today -------------------------------------------------------------


def forecast(w, student, group):
    w.db.expire_all()
    page = owner(w).get(f"{API}/org/payments/status", params={"limit": 200}).json()
    (row,) = [i for i in page["items"] if i["student_id"] == str(student.id) and i["group_id"] == str(group.id)]
    return row["lessons_left"], row["paid_through_forecast"]


def test_the_forecast_of_a_package_counts_from_the_pay_from_day(world, fixed_today):
    # B = Tue/Thu/Sat, today Tue 17.11, «Оплата с» Wed 18.11, 10 lessons paid (450 000 x 10 / 12 = 375 000): 19,21,24,26,28.11, 01,03,05,08,10.12 -> 10.12 (from today it said 08.12: it counted Tue 17.11)
    from tests.money1b_helpers import add as add_student
    from tests.money1b_helpers import fresh_student

    a, b, c = three(world, fixed_today, "lessons")
    s = fresh_student(world, "Предоплаченный")
    add_student(world, s, group=b, mode="add", pay_from="2026-11-18")
    pay(world, s, b, amount=375_000)
    assert forecast(world, s, b) == (10, "2026-12-10")


def test_the_forecast_after_a_transfer_with_twelve_lessons_counts_from_the_pay_from_day(world, fixed_today):
    # a transfer that carries 450 000 = 12 lessons of B (the same shape as a payment) from «Оплата с» 18.11: 19,21,24,26,28.11, 01,03,05,08,10,12,15.12 -> 15.12 (from today: 12.12)
    from tests.money1b_helpers import add as add_student
    from tests.money1b_helpers import fresh_student

    a, b, c = three(world, fixed_today, "lessons")
    s = fresh_student(world, "С переносом")
    add_student(world, s, group=b, mode="add", pay_from="2026-11-18")
    pay(world, s, b, amount=450_000)
    assert forecast(world, s, b) == (12, "2026-12-15")
