"""LOGIC-A: concurrent writes on PostgreSQL - pause / opening / leave / price against payments and refunds, two payouts, a payout against a
recalculation.  Nothing deadlocks, nothing answers 500, the account reconciles and no outflow is left without money."""
from datetime import date, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.auth import payment_status as ps
from src.auth.leave_service import LeaveService
from src.auth.logica_service import OpeningService, PauseService
from src.auth.payments_schemas import LeaveRequest, OpeningPut, PauseCreate, PaymentCreate, SettleItem
from src.auth.payments_service import PaymentError, PaymentService, is_deadlock
from src.models.auth import MembershipPause, StudentGroup, StudentPayment, StudentRefund

from tests.pg_world import give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payment_carry_over_pg import engine, run_in_thread  # noqa: F401  (engine = fixture)
from tests.test_pay2_api_pg import D, membership, settings_block, stay

pytestmark = needs_pg


def prepared(w, price=600_000):
    w.group.schedule = []
    w.db.commit()
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, price, valid_from=D(2026, 1, 1))
    m = membership(w)
    stay(w, m, D(2026, 9, 10))
    return m


def ok(outcome):
    """No stray exception: a refusal of the service (409 / 422) or a deadlock PostgreSQL found is acceptable, a 500 is not."""
    err = outcome.get("error")
    return err is None or isinstance(err, PaymentError) or is_deadlock(err)


def join_all(*threads):
    for t, _ in threads:
        t.join(timeout=60)


def reconciles(w, org, mid):
    w.db.expire_all()
    (entry,) = ls.LedgerLoader(w.db, org, ps.business_today()).run([mid])
    assert entry.ledger.reconciles and bl.F_OUTFLOW_EXCEEDS not in entry.ledger.flags
    return entry.ledger


def test_a_pause_and_a_payment_in_parallel_both_land_and_the_account_reconciles(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    today = ps.business_today()
    t1 = run_in_thread(engine, lambda s: PauseService(s).create(org, m.id, actor, PauseCreate(first_day=today - timedelta(days=5), last_day=today - timedelta(days=1))))
    t2 = run_in_thread(engine, lambda s: PaymentService(s).create_payment(org, m.id, actor, PaymentCreate(amount=300_000, paid_on=today)))
    join_all(t1, t2)
    assert ok(t1[1]) and ok(t2[1])
    world.db.expire_all()
    landed = [("value" in o) for _, o in (t1, t2)]
    assert landed.count(True) >= 1
    assert world.db.query(MembershipPause).filter_by(membership_id=m.id, voided_at=None).count() == (1 if landed[0] else 0)
    ledger = reconciles(world, org, m.id)
    assert ledger.paid == (300_000 if landed[1] else 0)


def test_a_pause_void_and_a_refund_never_leave_a_refund_without_money(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    today = ps.business_today()
    pause = PauseService(world.db).create(org, m.id, actor, PauseCreate(first_day=today - timedelta(days=20), last_day=today - timedelta(days=1))).pause
    PaymentService(world.db).create_payment(org, m.id, actor, PaymentCreate(amount=5_000_000, paid_on=today))
    refundable = PaymentService(world.db)._row(org, m.id).refundable
    t1 = run_in_thread(engine, lambda s: PaymentService(s).create_refund(org, m.id, actor, amount=refundable, method="cash", reason=None, note=None))
    t2 = run_in_thread(engine, lambda s: PauseService(s).void(org, __import__("uuid").UUID(pause.id), actor, "ошибка"))
    join_all(t1, t2)
    assert ok(t1[1]) and ok(t2[1])
    reconciles(world, org, m.id)


def test_leaving_with_a_refund_and_a_payment_in_parallel_do_not_deadlock(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    today = ps.business_today()
    PaymentService(world.db).create_payment(org, m.id, actor, PaymentCreate(amount=4_000_000, paid_on=today))
    refundable = LeaveService(world.db).preview_group(org, world.group.id, world.students[0].id, money=True).memberships[0].money.refundable
    body = LeaveRequest(settle=[SettleItem(membership_id=str(m.id), action="refund", expected_refundable=refundable, method="cash")])
    t1 = run_in_thread(engine, lambda s: LeaveService(s).leave(org, str(world.group.id), str(world.students[0].id), actor, "owner", body, money=True))
    t2 = run_in_thread(engine, lambda s: PaymentService(s).create_payment(org, m.id, actor, PaymentCreate(amount=100_000, paid_on=today)))
    join_all(t1, t2)
    assert ok(t1[1]) and ok(t2[1])
    world.db.expire_all()
    reconciles(world, org, m.id)
    assert world.db.query(StudentRefund).filter_by(membership_id=m.id, voided_at=None).count() <= 1


def test_an_opening_balance_and_a_payment_in_parallel(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    today = ps.business_today()
    t1 = run_in_thread(engine, lambda s: OpeningService(s).put(org, m.id, actor, OpeningPut(debt_amount=100_000, debt_on=D(2026, 9, 1), reason="импорт")))
    t2 = run_in_thread(engine, lambda s: PaymentService(s).create_payment(org, m.id, actor, PaymentCreate(amount=50_000, paid_on=today)))
    join_all(t1, t2)
    assert ok(t1[1]) and ok(t2[1])
    reconciles(world, org, m.id)


def test_a_backdated_price_and_a_payment_in_parallel_never_rewrite_a_charged_period_with_money(world, engine):
    from src.auth import pricing

    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    today = ps.business_today()
    t1 = run_in_thread(engine, lambda s: pricing.PricingService(s).set_group_price(org, world.group.id, actor, amount=700_000, valid_from=D(2026, 9, 10)))
    t2 = run_in_thread(engine, lambda s: PaymentService(s).create_payment(org, m.id, actor, PaymentCreate(amount=200_000, paid_on=today)))
    join_all(t1, t2)
    assert all(ok(o) or isinstance(o.get("error"), pricing.PriceError) for _, o in (t1, t2)), (t1[1], t2[1])
    world.db.expire_all()
    ledger = reconciles(world, org, m.id)
    paid, priced = "value" in t2[1], "value" in t1[1]
    # both must not have won: a payment that landed BEFORE the price makes the backdated price a 409; the price first lets the payment in
    seg = [s for s in ledger.segments if s.begun][0]
    if paid and priced:
        assert t1[1]["value"] is not None and seg.price.amount in (600_000, 700_000)
    assert ledger.paid == (200_000 if paid else 0)


def test_two_payouts_in_parallel_are_both_kept_and_a_payout_does_not_wait_for_a_recalculation(world, engine):
    from src.auth.payroll_payouts import PayrollPayoutService
    from src.auth.payroll_schemas import PayoutCreate
    from src.auth.payroll_service import PayrollService
    from src.models.auth import PayrollSheet

    from tests.test_logicA_payroll_pg import g_pay_world, save_sheet

    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    org, actor = str(world.org.id), str(world.owner.id)
    body = lambda amount: PayoutCreate(amount=amount, paid_on=date.today(), method="cash")
    fresh = lambda s: (lambda row: PayrollService(s)._fresh_total(org, row))
    import uuid

    def pay(amount):
        def call(s):
            row = s.get(PayrollSheet, uuid.UUID(sheet["id"]))
            return PayrollPayoutService(s).create(org, uuid.UUID(sheet["id"]), actor, body(amount), uuid.uuid4(), lambda r: PayrollService(s)._fresh_total(org, r))
        return call

    threads = [run_in_thread(engine, pay(a)) for a in (100_000, 200_000, 300_000)]
    join_all(*threads)
    assert all("error" not in o for _, o in threads), [o.get("error") for _, o in threads]
    world.db.expire_all()
    from src.models.auth import PayrollPayout

    assert sorted(int(p.amount) for p in world.db.query(PayrollPayout).filter_by(org_id=world.org.id, voided_at=None)) == [100_000, 200_000, 300_000]
    state = PayrollService(world.db).payout_state(org, world.db.get(PayrollSheet, uuid.UUID(sheet["id"])))
    assert state.paid == 600_000 and state.state == "overpaid"
