"""Money-1 pack B6 / B7 of the devil (PostgreSQL): a transfer carries the days that were paid for WITHOUT money.

«Остаток на начало» (the notebook: «оплачено до 20.11») is how a centre is moved into the system.  The transfer carried only the money of the account (``min(advance, money)`` = 0 here), so a student paid until
20.11 who moved to another group on 25.10 owed the new group at once and the paid days were lost (d7).  Now they come with him, like the money: derived, never stored."""
from datetime import date

from src.models.auth import MembershipOpeningBalance, StudentGroup

from tests.money1b_helpers import account, opening_until, transfer
from tests.pg_world import give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def the_world(w, fixed_today):
    fixed_today(D(2026, 10, 25))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000)
    other = make_group(w.db, w.org, w.teacher, "Вторая")
    give_group_price(w.db, other, 400_000)
    s = w.students[0]
    m = membership(w, s)
    stay(w, m, D(2026, 9, 25))
    opening_until(w, m, D(2026, 11, 20))                                  # from the notebook: paid until 20.11, no money in the system
    return s, m, other


def new_membership(w, s, group):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=group.id).one()


def test_b6_d7_the_days_paid_without_money_come_with_the_student(world, fixed_today):
    w = world
    s, m, other = the_world(w, fixed_today)
    done = transfer(w, s, other)
    assert {"code": "paid_days_carried_over", "message": "Оплаченные дни — по 20.11.2026 — перешли вместе с учеником"} in done["carried"]
    mb = new_membership(w, s, other)
    now = account(w, mb.id, D(2026, 10, 26))
    assert now.covered_through == D(2026, 11, 20) and now.charged == 0 and now.debt == 0         # before: a period of 400 000 from 26.10 and a debt at once
    first = account(w, mb.id, D(2026, 11, 21))                                                       # the first day after the paid days: the first period of the new group begins
    (seg,) = [x for x in first.segments if x.kind == "period"]
    assert (seg.period_start, seg.amount) == (D(2026, 11, 21), 400_000) and first.debt == 400_000     # the NEW price: the price is not carried (#105)
    old = account(w, m.id, D(2026, 10, 26))
    assert old.charged == account(w, m.id, D(2026, 10, 25)).charged                                  # the old group is closed on the transfer day: nothing more is charged there


def test_b6_the_dry_run_names_the_carried_days_and_writes_nothing(world, fixed_today):
    w = world
    s, m, other = the_world(w, fixed_today)
    done = transfer(w, s, other, dry_run=True)
    assert done["dry_run"] is True and any(c["code"] == "paid_days_carried_over" for c in done["carried"])
    w.db.expire_all()
    assert w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=other.id).count() == 0
    assert w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=w.group.id, is_active=True).count() == 1


def test_b6_nothing_is_carried_when_the_paid_days_are_over(world, fixed_today):
    w = world
    s, m, other = the_world(w, fixed_today)
    w.db.query(MembershipOpeningBalance).filter_by(membership_id=m.id).update({"paid_until": D(2026, 10, 24)})      # before the transfer day: nothing left to carry
    w.db.commit()
    done = transfer(w, s, other)
    assert not [c for c in done["carried"] if c["code"] == "paid_days_carried_over"]
    mb = new_membership(w, s, other)
    assert account(w, mb.id, D(2026, 10, 26)).covered_through is None or account(w, mb.id, D(2026, 10, 26)).debt > 0


def test_b6_the_days_go_on_along_a_chain_of_transfers_a_to_b_to_c(world, fixed_today):
    w = world
    s, m, other = the_world(w, fixed_today)
    third = make_group(w.db, w.org, w.teacher, "Третья")
    give_group_price(w.db, third, 500_000)
    transfer(w, s, other)
    fixed_today(D(2026, 10, 28))
    transfer(w, s, third)
    mc = new_membership(w, s, third)
    led = account(w, mc.id, D(2026, 10, 29))
    assert led.covered_through == D(2026, 11, 20) and led.debt == 0
    mb = new_membership(w, s, other)
    assert account(w, mb.id, D(2026, 10, 29)).debt == 0                                                # the middle group is closed: it was charged only the day he was there


def test_b6_a_transfer_of_a_student_with_money_still_carries_the_money_and_the_days_apart(world, fixed_today):
    from tests.test_pay2_api_pg import seed_payment

    w = world
    s, m, other = the_world(w, fixed_today)
    seed_payment(w, m, 300_000, D(2026, 10, 21))                                                       # real money: the first period after the paid days (21.11 - 20.12) paid
    before = account(w, m.id, D(2026, 10, 25))
    assert before.advance == 300_000 and before.refundable == 300_000
    transfer(w, s, other)
    mb = new_membership(w, s, other)
    led = account(w, mb.id, D(2026, 10, 26))
    assert led.transferred_in == 300_000 and led.covered_through >= D(2026, 11, 20) and led.debt == 0     # the advance comes as money; the days go as days: the new group is paid from 21.11 by that money
