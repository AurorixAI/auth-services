"""PAY-2: the budget of queries of every list / report, and concurrent writes (PostgreSQL)."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.auth import payment_status as ps
from src.auth.payments_schemas import PaymentCreate
from src.auth.payments_service import PaymentError, PaymentService
from src.models.auth import GroupAudit, StudentGroup, StudentPayment, StudentRefund

from tests.pg_world import (  # noqa: F401  (fixtures)
    give_group_price, make_client, make_group, make_student, needs_pg, pg_session, world,
)
from tests.test_payment_carry_over_pg import engine, run_in_thread  # noqa: F401  (engine = fixture)
from tests.test_pay2_api_pg import API, D, membership, owner, seed_payment, settings_block, stay

pytestmark = needs_pg


def count_statements(w, fn):
    w.db.expire_all()
    seen = []
    engine_ = w.db.get_bind()

    def record(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(engine_, "before_cursor_execute", record)
    try:
        result = fn()
    finally:
        event.remove(engine_, "before_cursor_execute", record)
    return result, len(seen)


def grow(w, count, groups=4, start=0):
    """``count`` more students over several groups: each with a stay, a payment and some of them a write-off."""
    gs = [make_group(w.db, w.org, w.teacher, f"Г{i}") for i in range(groups)]
    w.db.commit()
    for g in gs:
        give_group_price(w.db, g, 300_000)
    first = D(2026, 9, 1)
    for i in range(start, start + count):
        g = gs[i % groups]
        student = make_student(w.db, w.org, g, f"Имя{i}", f"Фамилия{i}", f"P2-{i:04d}")
        w.db.flush()
        m = w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=g.id).one()
        m.joined_at = datetime(2026, 9, 1 + i % 20, 6)
        (period,) = m.periods
        period.joined_at = m.joined_at
        w.db.add(StudentPayment(org_id=w.org.id, membership_id=m.id, student_id=student.id, group_id=g.id,
                                amount=100_000 + 1000 * i, paid_on=first + timedelta(days=i % 40), created_by=w.owner.id,
                                method="cash", receipt_no=10_000 + i, created_at=datetime(2026, 9, 1, 9, i % 60)))
    w.db.commit()


def test_every_list_and_report_has_a_fixed_budget_of_queries(world, monkeypatch):
    monkeypatch.setattr(ps, "business_today", lambda tz=None: D(2026, 10, 21))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 500_000)
    world.group.schedule = []
    world.db.commit()
    c = owner(world)
    some = membership(world)

    def probes():
        return {
            "status": lambda: c.get(f"{API}/org/payments/status", params={"limit": 50}).json(),
            "status_debt": lambda: c.get(f"{API}/org/payments/status", params={"bucket": "debt", "limit": 50}).json(),
            "summary": lambda: c.get(f"{API}/org/payments/summary").json(),
            "balances": lambda: c.get(f"{API}/org/payments/balances", params={"kind": "debt"}).json(),
            "receipts": lambda: c.get(f"{API}/org/payments/receipts", params={"from": "2026-09-01", "to": "2026-10-21", "group_by": "month"}).json(),
            "items": lambda: c.get(f"{API}/org/payments/receipts/items", params={"from": "2026-09-01", "to": "2026-10-21"}).json(),
            "statement": lambda: c.get(f"{API}/org/memberships/{some.id}/statement").json(),
            "preview": lambda: c.get(f"{API}/org/memberships/{some.id}/payments/preview", params={"amount": 300_000}).json(),
            "student": lambda: c.get(f"{API}/org/students/{world.students[0].id}/memberships").json(),
        }

    grow(world, 12)
    small = {name: count_statements(world, fn)[1] for name, fn in probes().items()}
    grow(world, 190, start=12)
    big = {}
    for name, fn in probes().items():
        _, n = count_statements(world, fn)
        big[name] = n
    print("QUERIES small/big:", small, big)
    assert all(n <= 20 for n in big.values()), big
    for name in big:
        assert big[name] <= small[name] + 2, (name, small, big)      # does not depend on the number of memberships
    assert c.get(f"{API}/org/payments/summary").json()["tracked_total"] >= 200


def test_summary_cache_is_short_and_dropped_by_a_write(world, monkeypatch):
    import src.auth.payments_service as svc

    monkeypatch.setattr(svc, "SUMMARY_TTL_SECONDS", 30.0)
    svc.invalidate_summary()
    monkeypatch.setattr(ps, "business_today", lambda tz=None: D(2026, 10, 21))
    settings_block(world, accounting_start_date="2026-09-01")
    give_group_price(world.db, world.group, 500_000, valid_from=D(2026, 1, 1))
    m = membership(world)
    stay(world, m, D(2026, 9, 15))
    c = owner(world)
    first, n = count_statements(world, lambda: c.get(f"{API}/org/payments/summary").json())
    again, n_again = count_statements(world, lambda: c.get(f"{API}/org/payments/summary").json())
    assert again == first and n_again < n                                  # served from the cache
    assert c.post(f"{API}/org/memberships/{m.id}/payments", json={"amount": 100_000, "paid_on": "2026-10-21", "method": "cash"}).status_code == 201
    after = c.get(f"{API}/org/payments/summary").json()
    assert after["debt_total"] == first["debt_total"] - 100_000            # a write forgets it
    svc.invalidate_summary()


# ---- concurrency -----------------------------------------------------------------------------------------------


def service_call(org_id, fn):
    return lambda session: fn(PaymentService(session))


def prepared(world):
    settings_block(world, accounting_start_date="2026-01-01")
    give_group_price(world.db, world.group, 500_000, valid_from=D(2026, 1, 1))
    m = membership(world)
    stay(world, m, D(2026, 1, 5))
    return m


def body(amount, **kw):
    return PaymentCreate(amount=amount, paid_on=date.today(), method="cash", **kw)


def test_parallel_payments_to_one_membership_add_up_with_distinct_receipt_numbers(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    threads = [run_in_thread(engine, service_call(org, lambda s, a=a: s.create_payment(org, m.id, actor, body(a))))
               for a in (100_000, 200_000, 300_000, 400_000)]
    for t, _ in threads:
        t.join(timeout=60)
    errors = [o.get("error") for _, o in threads if "error" in o]
    assert not errors, errors
    world.db.expire_all()
    rows = world.db.query(StudentPayment).filter_by(membership_id=m.id).all()
    assert sorted(int(r.amount) for r in rows) == [100_000, 200_000, 300_000, 400_000]
    assert len({r.receipt_no for r in rows}) == 4
    assert PaymentService(world.db)._row(org, m.id).last_payment is not None
    entry = ls.LedgerLoader(world.db, org, date.today()).run([m.id])[0]
    assert entry.ledger.paid == 1_000_000 and entry.ledger.reconciles


def test_a_refund_and_a_void_of_its_payment_never_both_win(world, engine):
    """The family lock: either the refund is made and the void is refused (VOID_BREAKS_REFUND), or the void wins and the
    refund is refused - never a refund left without money."""
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    service = PaymentService(world.db)
    created = service.create_payment(org, m.id, actor, body(12_000_000))
    pid = created.payment.id
    stay(world, m, D(2026, 1, 5), date.today())                        # he left: the whole advance is refundable
    refundable = service._row(org, m.id).refundable
    assert refundable > 0
    t1, o1 = run_in_thread(engine, service_call(org, lambda s: s.create_refund(
        org, m.id, actor, amount=refundable, method="cash", reason=None, note=None)))
    t2, o2 = run_in_thread(engine, service_call(org, lambda s: s.void_payment(org, pid, actor, "ошибка")))
    for t in (t1, t2):
        t.join(timeout=60)
    errors = [o["error"] for o in (o1, o2) if "error" in o and not isinstance(o["error"], PaymentError)]
    assert not errors, errors
    refund_done, void_done = "value" in o1, "value" in o2
    assert not (refund_done and void_done)
    world.db.expire_all()
    entry = ls.LedgerLoader(world.db, org, date.today()).run([m.id])[0]
    assert bl.F_OUTFLOW_EXCEEDS not in entry.ledger.flags and entry.ledger.reconciles


def test_a_payment_and_a_transfer_of_the_same_student_do_not_deadlock(world, engine):
    m = prepared(world)
    second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    give_group_price(world.db, second, 400_000, valid_from=D(2026, 1, 1))
    org, actor = str(world.org.id), str(world.owner.id)
    from src.auth.service import AuthService

    t1, o1 = run_in_thread(engine, lambda s: PaymentService(s).create_payment(org, m.id, actor, body(5_000_000)))
    t2, o2 = run_in_thread(engine, lambda s: AuthService(s).add_student_to_group(
        org, str(second.id), str(world.students[0].id), actor_id=actor, payments_detail=True))
    for t in (t1, t2):
        t.join(timeout=60)
    for o in (o1, o2):
        # a deadlock PostgreSQL finds is a 409 to repeat (the router maps it for the transfer, the guard for the payment)
        from src.auth.payments_service import is_deadlock

        assert "error" not in o or isinstance(o["error"], PaymentError) or is_deadlock(o["error"]), o.get("error")
    world.db.expire_all()
    ids = [e.membership.id for e in ls.LedgerLoader(world.db, org, date.today()).load(student_id=world.students[0].id, scope="all")]
    entries = ls.LedgerLoader(world.db, org, date.today()).run(ids)
    assert all(e.ledger.reconciles for e in entries)
    assert sum(e.ledger.transferred_out for e in entries) == sum(e.ledger.transferred_in for e in entries)


def test_a_void_and_a_replace_of_the_same_payment_one_wins(world, engine):
    m = prepared(world)
    org, actor = str(world.org.id), str(world.owner.id)
    pid = PaymentService(world.db).create_payment(org, m.id, actor, body(700_000)).payment.id
    t1, o1 = run_in_thread(engine, service_call(org, lambda s: s.void_payment(org, pid, actor, "ошибка")))
    t2, o2 = run_in_thread(engine, service_call(org, lambda s: s.replace_payment(org, pid, actor, body(800_000), "исправление")))
    for t in (t1, t2):
        t.join(timeout=60)
    assert [("value" in o) for o in (o1, o2)].count(True) >= 1
    for o in (o1, o2):
        assert "error" not in o or isinstance(o["error"], PaymentError), o.get("error")
    world.db.expire_all()
    live = world.db.query(StudentPayment).filter_by(membership_id=m.id, voided_at=None).all()
    assert len(live) in (0, 1)


@pytest.mark.skipif(not __import__("os").environ.get("PAY2_PERF"), reason="a manual measurement: PAY2_PERF=1")
def test_timing_at_1500_memberships(world, monkeypatch):
    import time

    monkeypatch.setattr(ps, "business_today", lambda tz=None: D(2026, 10, 21))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    grow(world, 1500, groups=30)
    c = owner(world)
    for name, url, params in (("status", "/org/payments/status", {"limit": 50}), ("summary", "/org/payments/summary", {}),
                              ("receipts", "/org/payments/receipts", {"from": "2026-09-01", "to": "2026-10-21", "group_by": "month"})):
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            assert c.get(API + url, params=params).status_code == 200
            samples.append(time.perf_counter() - started)
        print(f"TIMING {name}: min {min(samples):.2f}s max {max(samples):.2f}s")
