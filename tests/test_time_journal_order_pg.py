"""TIME-TESTS (PostgreSQL), round 8.2 / 8.3: the lines of the journal of ONE action keep the order they were written in, and a leave is ONE moment.

Before the stand clock a journal line took the clock of its own insert, so the lines of one action (a forgiveness and a refund of one archiving) were in the order
of writing and the journal of decisions (``created_at desc, id desc``) listed the later one first.  With the one moment of the action the lines would tie and the order
would be that of the uuid; the lines now take ``moment + 1 us``, ``+ 2 us`` ... in the order of writing.  The expectations below were read from ``money-2`` (the
settlement writes its lines in the order of the choices; the leave line itself was written with the clock of the database = the START of the transaction, so before them;
the archive lines carry the archiving instant, the settlement lines come after) and the ``ticking`` variants were run unchanged on a ``money-2`` tree.
The ``frozen`` variants stand the clock still (equal moments), which is what the one-moment rule makes of the old behaviour on the new branch.
"""
from datetime import date, datetime, timedelta

import pytest

from src.auth import business_clock as bc
from src.auth import payment_status as ps
from src.models.auth import GroupAudit, Student, StudentRefund, StudentWriteoff

from tests.money1b_helpers import add
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_stage2fix_pg import leave, window
from tests.test_money2_transfer_a_pg import DAY, groups3
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, stay  # noqa: F401  (fixture)

MOMENT = datetime(DAY.year, DAY.month, DAY.day, 12, 0, tzinfo=bc.BUSINESS_TIMEZONE)


def test_the_moments_of_an_action_follow_the_order_of_the_calls_by_a_microsecond():
    stamps = bc.action_moments(datetime(2026, 11, 20, 7, 0))
    got = [stamps.next() for _ in range(3)]
    assert got == [datetime(2026, 11, 20, 7, 0, 0, k) for k in (1, 2, 3)]


def _two_debts_and_an_advance(w, fixed_today):
    """Alia: A (300 000) unpaid since the start of the accounting = a debt; B (400 000) paid ahead = an advance to give back.  Returns the groups."""
    a, b, c = groups3(w, fixed_today)
    s0 = w.students[0]
    add(w, s0, group=b, mode="add")
    stay(w, membership(w, s0, b), date(2026, 11, 2))
    pay(w, s0, b, amount=1_000_000)
    return a, b, s0


def _clock(frozen):
    if frozen:
        bc.set_clock(lambda: MOMENT)                  # the clock stands still: every reading is the same instant


def _journal(w):
    r = owner(w).get(f"{API}/org/payments/decisions", params={"from": DAY.isoformat(), "to": DAY.isoformat()})
    assert r.status_code == 200, r.text
    return [x["kind"] for x in r.json()["rows"] if x["kind"] in ("payment_writeoff", "payment_refund")]


@pytest.mark.parametrize("frozen", [False, True], ids=["ticking", "frozen"])
@pytest.mark.parametrize("order", ["forgive_first", "refund_first"])
def test_an_archiving_with_two_settlement_lines_lists_them_in_the_order_of_writing(world, fixed_today, frozen, order):
    a, b, s0 = _two_debts_and_an_advance(world, fixed_today)
    pv = owner(world).get(f"{API}/org/students/{s0.id}/archive-preview").json()["memberships"]
    by_group = {m["group_id"]: m for m in pv}
    debt, refundable = by_group[str(a.id)]["money"]["debt"], by_group[str(b.id)]["money"]["refundable"]
    assert debt > 0 and refundable > 0, pv
    forgive = {"membership_id": by_group[str(a.id)]["membership_id"], "action": "writeoff", "expected_debt": debt, "reason": "договорённость"}
    refund = {"membership_id": by_group[str(b.id)]["membership_id"], "action": "refund", "expected_refundable": refundable, "method": "cash"}
    items = [forgive, refund] if order == "forgive_first" else [refund, forgive]
    _clock(frozen)
    r = owner(world).post(f"{API}/org/students/{s0.id}/archive", json={"reason": "переехал", "settle": items})
    assert r.status_code == 200, r.text
    written = ["payment_writeoff" if i["action"] == "writeoff" else "payment_refund" for i in items]
    assert _journal(world) == list(reversed(written))                       # the journal shows the later line first, as it always did
    world.db.expire_all()
    student = world.db.get(Student, s0.id)
    lines = world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "student_archived").all()
    assert lines and {x.created_at for x in lines} == {student.archived_at}  # the archive lines ARE the archiving instant (student_archive.archived_with_rows)
    settle = world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action.in_(("payment_writeoff", "payment_refund"))).all()
    assert settle and all(x.created_at > student.archived_at for x in settle)            # the settlement lines come after the archive lines, as on money-2
    if frozen:                                                              # the data rows stay on the exact moment; only the journal lines moved by microseconds
        wo, rf = world.db.query(StudentWriteoff).filter_by(org_id=world.org.id).one(), world.db.query(StudentRefund).filter_by(org_id=world.org.id).one()
        assert wo.created_at == rf.created_at == student.archived_at
        assert max(x.created_at for x in settle) - student.archived_at == timedelta(microseconds=2)


@pytest.mark.parametrize("frozen", [False, True], ids=["ticking", "frozen"])
def test_a_leave_writes_its_own_line_before_the_settlement_line(world, fixed_today, frozen):
    a, b, s0 = _two_debts_and_an_advance(world, fixed_today)
    row = window(world, s0, a)
    _clock(frozen)
    leave(world, s0, a, {"settle": [{"membership_id": row["membership_id"], "action": "writeoff", "expected_debt": row["money"]["debt"], "reason": "договорённость"}]})
    world.db.expire_all()
    raw = (world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action.in_(("student_left_group", "payment_writeoff")))
           .order_by(GroupAudit.created_at, GroupAudit.id).all())
    assert [x.action for x in raw] == ["student_left_group", "payment_writeoff"]
    assert _journal(world) == ["payment_writeoff"]                         # the journal of decisions has the forgiveness, not the leave line
    if frozen:
        wo = world.db.query(StudentWriteoff).filter_by(org_id=world.org.id).one()
        assert raw[0].created_at == wo.created_at == membership(world, s0, a).left_at                  # one moment: the stay, the data row, the leave line
        assert raw[1].created_at == raw[0].created_at + timedelta(microseconds=1)
