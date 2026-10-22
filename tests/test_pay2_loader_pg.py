"""PAY-2 stream A: ``ledger_service`` - the database becomes ``LedgerInput`` (PostgreSQL)."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.models.auth import (
    Group,
    MembershipTransfer,
    Organization,
    Student,
    StudentGroup,
    StudentPayment,
    StudentRefund,
    StudentWriteoff,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    give_group_price, make_group, make_student, needs_pg, pg_session, world,
)

pytestmark = needs_pg
D = date


def set_payments_settings(db, org, **block):
    settings = dict(org.settings or {})
    settings["payments"] = {**settings.get("payments", {}), **block}
    org.settings = settings
    db.commit()


def membership_of(db, student, group):
    return db.query(StudentGroup).filter_by(student_id=student.id, group_id=group.id).one()


def join_on(db, membership, day, hour=6, left=None):
    """Set the stay of the membership (its single period and the mirror columns)."""
    membership.joined_at = datetime(day.year, day.month, day.day, hour)
    if left is not None:
        membership.left_at = datetime(left.year, left.month, left.day, hour)
        membership.is_active = False
    (period,) = membership.periods
    period.joined_at, period.left_at = membership.joined_at, membership.left_at
    db.commit()


def add_payment(db, world, membership, amount, paid_on, *, created=0):
    p = StudentPayment(
        org_id=world.org.id, membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
        amount=amount, paid_on=paid_on, created_by=world.owner.id, method="cash",
        created_at=datetime(paid_on.year, paid_on.month, paid_on.day, 9, created),
    )
    db.add(p)
    db.commit()
    return p


def loader(world, today):
    world.db.expire_all()
    return ls.LedgerLoader(world.db, str(world.org.id), today)


def test_e1_in_the_database(world):
    db, student, group = world.db, world.students[0], world.group
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01")
    give_group_price(db, group, 500_000)
    m = membership_of(db, student, group)
    join_on(db, m, D(2026, 9, 15))
    add_payment(db, world, m, 231_000, D(2026, 9, 20))
    (row,) = loader(world, D(2026, 10, 21)).load(membership_id=m.id)
    l = row.ledger
    assert (l.debt, l.past_debt, l.current_due) == (769_000, 269_000, 500_000)
    assert l.status.paid_until == D(2026, 9, 27) and l.status.days_overdue == 24
    assert row.price.amount == 500_000 and len(row.payments) == 1
    # a write-off and a refund are read too (and voided ones are not)
    db.add(StudentWriteoff(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id,
                           amount=69_000, reason="болезнь", effective_on=D(2026, 10, 20), created_by=world.owner.id))
    db.add(StudentWriteoff(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id,
                           amount=5_000, reason="отменено", effective_on=D(2026, 10, 20), created_by=world.owner.id,
                           voided_at=datetime(2026, 10, 20), voided_by=world.owner.id, void_reason="ошибка"))
    db.commit()
    (row,) = loader(world, D(2026, 10, 21)).load(membership_id=m.id)
    assert row.ledger.debt == 700_000 and row.ledger.written_off == 69_000 and row.ledger.reconciles


def test_accounting_start_default_is_the_day_the_centre_was_created(world):
    start = ls.load_settings(world.db, str(world.org.id)).accounting_start
    from src.auth import payment_status as ps

    assert start == (ps.business_date(world.org.created_at) if world.org.created_at else start)       # the day in Tashkent, not the UTC date of the stamp
    set_payments_settings(world.db, world.org, accounting_start_date="2026-01-05")
    assert ls.load_settings(world.db, str(world.org.id)).accounting_start == D(2026, 1, 5)


def test_billing_history_gives_the_regimes(world):
    set_payments_settings(world.db, world.org, period_mode="calendar", billing_anchor="fixed_day", billing_day=10,
                          billing_history=[{"from": "min", "period_mode": "calendar", "billing_anchor": "payment_date"},
                                           {"from": "2026-10-01", "period_mode": "calendar", "billing_anchor": "fixed_day",
                                            "billing_day": 10}])
    regimes = ls.load_settings(world.db, str(world.org.id)).regimes
    assert [(r.effective_from, r.anchor, r.billing_day) for r in regimes] == [
        (date.min, "payment_date", 1), (D(2026, 10, 1), "fixed_day", 10)]


def test_transfer_between_groups_e4(world):
    db = world.db
    student = world.students[0]
    group_a = world.group
    group_b = make_group(db, world.org, world.teacher, "Группа B")
    group_a.schedule = group_b.schedule = []      # no lessons: an incomplete period is charged by days
    db.commit()
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01", period_mode="calendar",
                          billing_anchor="fixed_day", billing_day=1)
    give_group_price(db, group_a, 500_000)
    give_group_price(db, group_b, 400_000)
    a = membership_of(db, student, group_a)
    join_on(db, a, D(2026, 10, 1), left=D(2026, 10, 11))
    b = StudentGroup(student_id=student.id, group_id=group_b.id, is_active=True,
                     joined_at=datetime(2026, 10, 11, 6))
    db.add(b)
    db.commit()
    add_payment(db, world, a, 500_000, D(2026, 10, 1))
    db.add(MembershipTransfer(org_id=world.org.id, student_id=student.id, from_membership_id=a.id, to_membership_id=b.id,
                              at=datetime(2026, 10, 11, 6), created_by=world.owner.id))
    db.commit()
    rows = {r.membership.id: r for r in loader(world, D(2026, 10, 25)).load(membership_ids=[a.id, b.id], scope="all")}
    la, lb = rows[a.id].ledger, rows[b.id].ledger
    assert la.transferred_out == 323_000 and la.debt == 0 and la.advance == 0 and la.charged == 177_000
    assert lb.transferred_in == 323_000 and lb.charged == 258_000 and lb.advance == 65_000
    assert lb.status.paid_until == D(2026, 11, 4) and la.reconciles and lb.reconciles
    assert lb.first_billable == D(2026, 10, 12)
    # asking only for B still brings A in (transitively): the same answer
    (only_b,) = loader(world, D(2026, 10, 25)).load(membership_id=b.id)
    assert only_b.ledger.advance == 65_000
    # voiding the payment in A takes the money away from B: everything is derived
    db.query(StudentPayment).filter_by(id=rows[a.id].payments[0].id).update(
        {"voided_at": datetime(2026, 10, 12), "voided_by": world.owner.id, "void_reason": "ошибка"})
    db.commit()
    (b_after,) = loader(world, D(2026, 10, 25)).load(membership_id=b.id)
    assert b_after.ledger.transferred_in == 0 and b_after.ledger.debt == 258_000


def test_scope_tracked_vs_all(world):
    db = world.db
    set_payments_settings(db, world.org, accounting_start_date="2026-01-01")
    left = membership_of(db, world.students[1], world.group)
    join_on(db, left, D(2026, 3, 1), left=D(2026, 3, 20))
    archived = world.students[2]
    archived.status = "archived"
    db.commit()
    lo = loader(world, D(2026, 10, 21))
    tracked = set(lo.select(scope="tracked"))
    everything = set(lo.select(scope="all"))
    assert membership_of(db, world.students[0], world.group).id in tracked
    assert left.id not in tracked and left.id in everything          # left after the accounting start: its debt matters
    assert membership_of(db, archived, world.group).id not in tracked and membership_of(db, archived, world.group).id in everything
    # a membership that left before the accounting start and has no money record is not interesting
    old = membership_of(db, world.students[1], world.group)
    join_on(db, old, D(2025, 3, 1), left=D(2025, 3, 20))
    assert old.id not in set(loader(world, D(2026, 10, 21)).select(scope="all"))
    add_payment(db, world, old, 1000, D(2026, 1, 5))
    assert old.id in set(loader(world, D(2026, 10, 21)).select(scope="all"))


def test_query_count_does_not_depend_on_the_number_of_memberships(world):
    db = world.db
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01", period_mode="calendar",
                          billing_anchor="fixed_day", billing_day=1)
    give_group_price(db, world.group, 500_000)
    groups = [make_group(db, world.org, world.teacher, f"Г{i}") for i in range(4)]
    for g in groups:
        give_group_price(db, g, 300_000)
    for i in range(60):
        student = make_student(db, world.org, groups[i % 4], f"Ученик{i}", f"Фамилия{i}")
        m = membership_of(db, student, groups[i % 4])
        join_on(db, m, D(2026, 9, 10) + timedelta(days=i % 20))
        add_payment(db, world, m, 100_000 + i, D(2026, 10, 1))
    db.commit()

    def count_for(limit):
        lo = loader(world, D(2026, 10, 21))
        ids = lo.select(scope="all")[:limit]
        statements = []

        def on_exec(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", on_exec)
        try:
            rows = lo.run(ids)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", on_exec)
        assert len(rows) == len(ids)
        return len(statements)

    small, big = count_for(5), count_for(60)
    assert big <= 20 and abs(big - small) <= 3, (small, big)


def test_lessons_mode_counts_held_lessons(world):
    from tests.pg_world import excused_lesson, reconcile, save

    db = world.db
    set_payments_settings(db, world.org, period_mode="lessons", lessons_per_payment=4, accounting_start_date="2020-01-01",
                          billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 4}])
    give_group_price(db, world.group, 400_000)
    s1, s2, s3 = world.students
    for offset in (3, 2):
        day, lesson_id = reconcile(world, offset)
        save(world, day, lesson_id, [(s1, "present"), (s2, "present"), (s3, "absent")], status="completed")
    m = membership_of(db, s1, world.group)
    today = datetime.utcnow().date() + timedelta(days=1)
    lo = ls.LedgerLoader(db, str(world.org.id), today)
    (row,) = lo.load(membership_id=m.id)
    assert row.ledger.mode == bl.LESSONS and row.ledger.lessons.used == 2
    assert row.ledger.charged == 200_000 and row.ledger.debt == 200_000      # 2 lessons of 100 000, nothing paid
    assert [c.lesson_id for c in row.ledger.counted] and not any(row.ledger.lessons.paid_flags)
    add_payment(db, world, m, 100_000, today)
    (row,) = ls.LedgerLoader(db, str(world.org.id), today).load(membership_id=m.id)
    assert row.ledger.lessons.covered == 1 and row.ledger.lessons.unpaid == 1 and row.ledger.debt == 100_000


def test_a_same_day_round_trip_gives_the_same_money_whichever_membership_is_asked_first(world):
    """A -> B at 10:00 and B -> A at 15:00 the same day: the amounts depend on the order of the links, never on which
    membership the loader evaluates first (no cycle guard value is memoised); transfers out == transfers in (I7)."""
    from src.models.auth import StudentGroupPeriod

    db, student, group_a = world.db, world.students[0], world.group
    group_b = make_group(db, world.org, world.teacher, "Группа B")
    group_a.schedule = group_b.schedule = []
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1,
                          period_mode="calendar")
    give_group_price(db, group_a, 500_000)
    give_group_price(db, group_b, 400_000)
    db.commit()
    a = membership_of(db, student, group_a)
    t1, t2 = datetime(2026, 10, 11, 5, 0), datetime(2026, 10, 11, 10, 0)       # 10:00 and 15:00 Tashkent
    a.joined_at = datetime(2026, 10, 1, 6)
    (period,) = a.periods
    period.joined_at, period.left_at = a.joined_at, t1
    db.add(StudentGroupPeriod(membership_id=a.id, joined_at=t2))
    b = StudentGroup(student_id=student.id, group_id=group_b.id, is_active=True, joined_at=t1, left_at=None)
    db.add(b)
    db.flush()
    bp = b.periods[0]
    bp.joined_at, bp.left_at = t1, t2
    add_payment(db, world, a, 3_000_000, D(2026, 10, 1))
    db.add(MembershipTransfer(org_id=world.org.id, student_id=student.id, from_membership_id=a.id, to_membership_id=b.id,
                              at=t1, created_by=world.owner.id))
    db.add(MembershipTransfer(org_id=world.org.id, student_id=student.id, from_membership_id=b.id, to_membership_id=a.id,
                              at=t2, created_by=world.owner.id))
    db.commit()

    def totals(order):
        db.expire_all()
        rows = ls.LedgerLoader(db, str(world.org.id), D(2026, 10, 25)).run(order)
        by = {r.membership.id: r.ledger for r in rows}
        return {m: (l.transferred_in, l.transferred_out, l.debt, l.advance, l.charged) for m, l in by.items()}, by

    first, by = totals([a.id, b.id])
    second, _ = totals([b.id, a.id])
    only_a, _ = totals([a.id])
    only_b, _ = totals([b.id])
    assert first == second
    assert only_a[a.id] == first[a.id] and only_b[b.id] == first[b.id]
    assert sum(v[0] for v in first.values()) == sum(v[1] for v in first.values())          # I7
    assert all(l.reconciles for l in by.values())
    assert by[b.id].transferred_in > 0 and by[b.id].advance == 0 and by[b.id].debt == 0     # nothing is stranded in B
    assert by[a.id].transferred_in == by[b.id].transferred_out
