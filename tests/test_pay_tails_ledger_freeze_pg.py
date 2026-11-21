"""LEDGER-FREEZE facts (CHAIN_SWEEP.md, section 2, chains 1 and 2): PRINT AND PIN the numbers the code gives TODAY - they are NOT fixed in package A.

Stand: «Общий месяц» from the 1st, G1 = Tue + Thu, 800 000, November 2026: 3, 5, 10, 12, 17, 19, 24, 26 = 8 lessons of 100 000.  The new slot is a Wednesday valid from 14.11 (4, 11, 18, 25 = 4 more lessons in the
pattern of the month).  The denominator of a closed stay (the full pattern of the month) moves when the schedule of the group is edited in the middle of the month: the same class of error as the archive (package A
fixes only the archive); the decision «freeze the denominator at the moment of the charge or count by the schedule on the date» is the task LEDGER-FREEZE."""
from datetime import date

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)

pytestmark = needs_pg
D = date
TT = [slot("s2", 2, "2026-01-01", "14:00", "15:00"), slot("s4", 4, "2026-01-01", "14:00", "15:00")]
WED = slot("s3", 3, "2026-11-14", "14:00", "15:00")


def stand(w, fixed_today, **block):
    fixed_today(D(2026, 11, 2))
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1, **block)
    g1 = make_group(w.db, w.org, w.teacher, "G1")
    give_group_price(w.db, g1, 800_000)
    set_schedule(w, TT, g1)
    ensure_billing(w, g1, price=None)
    s = fresh_student(w, "Цепочка")
    add(w, s, group=g1, mode="add")
    return g1, s


def led(w, s, g, day):
    return account(w, membership(w, s, g).id, day)


def test_chain_1_a_transfer_after_four_lessons_then_a_slot_is_added_in_the_old_group(world, fixed_today):
    # hand: paid 800 000, four lessons (3, 5, 10, 12) = 400 000 charged in G1, 400 000 move to G2 on 12.11; after the slot is added nothing about G1 should change.
    g1, s = stand(world, fixed_today)
    g2 = make_group(world.db, world.org, world.teacher, "G2")
    give_group_price(world.db, g2, 800_000)
    set_schedule(world, [slot("s2", 2, "2026-01-01", "16:00", "17:00"), slot("s4", 4, "2026-01-01", "16:00", "17:00")], g2)
    ensure_billing(world, g2, price=None)
    fixed_today(D(2026, 11, 3))
    pay(world, s, g1, amount=800_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 12))
    add(world, s, group=g2, mode="transfer", from_group_id=str(g1.id))
    before = led(world, s, g1, D(2026, 11, 12))
    moved = led(world, s, g2, D(2026, 11, 13)).transferred_in
    fixed_today(D(2026, 11, 14))
    set_schedule(world, [*TT, WED], g1)
    after = led(world, s, g1, D(2026, 11, 14))
    moved_after = led(world, s, g2, D(2026, 11, 14)).transferred_in
    # FACT (LEDGER-FREEZE, not fixed here): before the slot G1 charges 400 000 and 400 000 moved; after it the denominator is 10 (8 + the Wednesdays 18, 25 inside the pattern from 14.11) -> G1 320 000 and
    # 480 000 «moved» (the carried sum is derived: 800 000 - 320 000).  The money of the student in G2 grows by 80 000 without any act of his.  Hand expectation: both stay 400 000.
    assert (before.charged, before.transferred_out, moved) == (400_000, 400_000, 400_000)
    assert (after.charged, after.transferred_out, moved_after) == (320_000, 480_000, 480_000)


def test_chain_2_a_cancelled_lesson_deduction_refunded_then_a_slot_is_added(world, fixed_today):
    # hand: paid 800 000; the centre cancels Thu 05.11 -> deduction 100 000 (800 000 / 8); refund 100 000; then a Wednesday is added: nothing about the account should change (no debt)
    g1, s = stand(world, fixed_today, recalc_history=[{"from": "min", "on": True}])
    fixed_today(D(2026, 11, 3))
    pay(world, s, g1, amount=800_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 4))
    r = owner(world).post(f"{API}/org/groups/{g1.id}/attendance", json={"lessonDate": "2026-11-05", "lessonId": "s4", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 6))
    before = led(world, s, g1, D(2026, 11, 6))
    assert (before.recalculated, before.advance, before.debt) == (100_000, 100_000, 0)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g1).id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "возврат за отмену"})
    assert r.status_code == 201, r.text
    fixed_today(D(2026, 11, 14))
    set_schedule(world, [*TT, WED], g1)
    after = led(world, s, g1, D(2026, 11, 14))
    # FACT (LEDGER-FREEZE, not fixed here): the lesson was 100 000 (800 000 / 8) and became 80 000 (800 000 / 10) after the slot: the deduction 80 000 < the refund 100 000 -> a false debt of 20 000.
    assert (after.recalculated, after.advance, after.debt, after.charged) == (80_000, 0, 20_000, 800_000)
    assert [(x.amount, x.lessons_in_period) for x in after.segments] == [(800_000, 10)]
