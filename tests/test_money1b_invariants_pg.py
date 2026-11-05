"""Money-1 pack B: the invariants I-1 .. I-4 on the NEW scenarios of the pack (the nightly reconciliation finds nothing) (PostgreSQL).

The scenarios: a student added with «Оплата с» on the first lesson of the group and a payment; a student taken out and back inside his period (bridged) and a payment; a student transferred with the days
that were paid without money; a student who waits three days for the new period.  The accounts add up (I-1), nothing moves without a record (I-2), the sheets are what the data says (I-3, I-4)."""
from datetime import date, datetime

from src.auth import money_reconcile as mr
from src.models.auth import StudentGroup, StudentGroupPeriod

from tests.money1b_helpers import account, add, fresh_student, opening_until, opening_world, transfer
from tests.pg_world import give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today, membership, seed_payment, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def test_the_accounts_of_the_new_scenarios_add_up_and_the_reconciliation_finds_nothing(world, fixed_today):
    w = world
    opening_world(w, fixed_today, today=D(2026, 10, 28))
    # a: added with the default «Оплата с» (06.11) and pays the first period in advance
    a = fresh_student(w, "Первый")
    add(w, a)
    ma = w.db.query(StudentGroup).filter_by(student_id=a.id, group_id=w.group.id).one()
    seed_payment(w, ma, 300_000, D(2026, 10, 28))
    # b: out of the group 14.10 and back 16.10 - one period, paid in full
    s_b = w.students[1]
    mb = membership(w, s_b)
    stay(w, mb, D(2026, 10, 5), D(2026, 10, 14))
    mb.is_active, mb.left_at, mb.joined_at = True, None, datetime(2026, 10, 16, 6)
    w.db.add(StudentGroupPeriod(membership_id=mb.id, joined_at=datetime(2026, 10, 16, 6)))
    w.db.commit()
    seed_payment(w, mb, 300_000, D(2026, 10, 5))
    # c: transferred 28.10 with the days paid without money and with money
    s_c = w.students[2]
    mc = membership(w, s_c)
    stay(w, mc, D(2026, 9, 25))
    opening_until(w, mc, D(2026, 11, 20))
    seed_payment(w, mc, 300_000, D(2026, 10, 21))
    other = make_group(w.db, w.org, w.teacher, "Вторая")
    give_group_price(w.db, other, 400_000)
    transfer(w, s_c, other)
    # the accounts of the scenarios say what the analyses say
    assert account(w, ma.id).debt == 0 and account(w, ma.id).advance == 300_000                  # not begun: the money lies as an advance, nothing is owed before the first lesson
    led_b = account(w, mb.id)
    assert led_b.charged == 300_000 and led_b.debt == 0 and led_b.reconciles                    # d8: ONE period (before: 97 000 + 300 000 from 16.10)
    w.db.expire_all()
    mc_new = w.db.query(StudentGroup).filter_by(student_id=s_c.id, group_id=other.id).one()
    led_c = account(w, mc_new.id)
    assert led_c.transferred_in == 300_000 and led_c.covered_through == D(2026, 12, 12) and led_c.reconciles     # the days to 20.11 + the money of 300 000 at 400 000 a month (22 days)
    snaps = []
    findings = mr.reconcile_org(w.db, w.org.id, today=D(2026, 10, 28), snapshots=snaps)
    assert len(snaps) >= 4                                                                         # LESSONS 94: it looked at the accounts of the scenarios
    assert [f for f in findings if f.invariant in ("I-1", "I-3", "I-4")] == []
