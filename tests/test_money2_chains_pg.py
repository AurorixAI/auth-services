"""Money-2: honest life of the money gives NO finding of I-1 / I-2 / I-5 after any event, and the new lines of the journal (payouts, transfer, cut pause) are written (PostgreSQL).

Strict: the list of findings of the centre is compared with ``[]``, after a check that the night really looked at rows (``rows`` > 0).  The distortions are in ``test_money2_rows_pg.py``."""
from datetime import date, timedelta

import pytest

from src.auth import money_reconcile as mr
from src.models.auth import (
    GroupAudit, MembershipPause, MembershipTransfer, MoneyReconcileCentre, MoneyRowBaseline, OrgMoneyAudit, PayrollPayout, StudentPayment,
)

from tests.pg_world import business_today, ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_rows_pg import night, pid_of
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def quiet(world, label):
    """A night after an event: the centre was checked, rows were compared, and nothing (I-1, I-2, I-5) is found."""
    result = night(world)
    assert result["failed"] == [], label
    assert result["rows"] > 0 or label.startswith("night 0"), label          # the first night of an empty centre has nothing to take
    ours = [(i["invariant"], i["key"]) for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] in ("I-1", "I-2", "I-5")]
    assert ours == [], label
    return result


def test_the_life_of_payments_a_correction_a_forgiveness_a_refund_an_opening_and_a_price_is_quiet(world):
    ensure_billing(world)
    s0, s1, s2 = world.students
    client = owner(world)
    quiet(world, "night 0")
    first = pay(world, s0, amount=450_000)
    quiet(world, "a payment")
    second = pay(world, s0, amount=300_000)
    assert client.post(f"{API}/org/payments/{pid_of(second)}/void", json={"reason": "Внесли дважды"}).status_code == 200
    quiet(world, "a second payment and its cancellation")
    replaced = client.post(f"{API}/org/payments/{pid_of(first)}/replace", json={"amount": 500_000, "paid_on": business_today().isoformat(), "method": "cash", "reason": "Неверная сумма"})
    assert replaced.status_code == 201, replaced.text
    quiet(world, "a correction")
    gone = pay(world, s1, amount=100_000)                                                          # created AND cancelled between two nights: it is in the baseline already cancelled
    assert client.post(f"{API}/org/payments/{pid_of(gone)}/void", json={"reason": "Ошибка кассира"}).status_code == 200
    quiet(world, "a payment created and cancelled between the nights")
    w = client.post(f"{API}/org/memberships/{mid(world, s1)}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    assert w.status_code == 201, w.text
    quiet(world, "a forgiveness")
    assert client.post(f"{API}/org/writeoffs/{w.json()['writeoff']['id']}/void", json={"reason": "Ошибка"}).status_code == 200
    quiet(world, "the cancellation of the forgiveness")
    pay(world, s0, amount=1_500_000)
    refund = client.post(f"{API}/org/memberships/{mid(world, s0)}/refunds", json={"amount": 50_000, "method": "cash", "reason": "Выбыл на неделю"})
    assert refund.status_code == 201, refund.text
    quiet(world, "a refund")
    assert client.post(f"{API}/org/refunds/{refund.json()['refund']['id']}/void", json={"reason": "Передумали"}).status_code == 200
    quiet(world, "the cancellation of the refund")
    url = f"{API}/org/memberships/{mid(world, s2)}/opening-balance"
    on = (business_today() - timedelta(days=76)).isoformat()
    assert client.put(url, json={"debt_amount": 250_000, "debt_on": on}).status_code == 200
    quiet(world, "an opening balance")
    assert client.put(url, json={"debt_amount": 200_000, "debt_on": on, "reason": "Уточнили сумму долга"}).status_code == 200
    quiet(world, "the edit of the opening balance (it is a cancellation plus a new row)")
    assert client.put(url, json={"reason": "Долга нет"}).status_code == 200
    quiet(world, "the opening balance taken off")


def test_a_transfer_with_money_and_a_correction_in_the_old_group_are_quiet_and_the_transfer_is_named_in_the_journal(world):
    from tests.money1b_helpers import transfer
    from tests.test_money1_reconcile_round_pg import transfer_world
    from tests.test_pay2_api_pg import membership

    s0, other = transfer_world(world)
    first = pay(world, s0, amount=1_500_000)                                                        # more than the group has charged: an advance travels
    quiet(world, "night 0")
    transfer(world, s0, other)
    quiet(world, "the transfer (the money travels, the link is a new row)")
    link = world.db.query(MembershipTransfer).filter_by(org_id=world.org.id).one()
    (line,) = world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="student_transferred").all()
    assert line.snapshot["transfer_id"] == str(link.id) and line.snapshot["from_membership_id"] == str(link.from_membership_id) and line.snapshot["to_membership_id"] == str(link.to_membership_id)
    assert line.snapshot["amount"] > 0 and line.group_id == other.id and line.actor_id == world.owner.id and line.actor_role == "owner"
    replaced = owner(world).post(f"{API}/org/payments/{pid_of(first)}/replace", json={"amount": 1_200_000, "paid_on": business_today().isoformat(), "method": "cash", "reason": "Неверная сумма"})
    assert replaced.status_code == 201, replaced.text
    quiet(world, "a correction of the payment of the OLD group after the transfer")
    pay(world, s0, other, amount=100_000)
    quiet(world, "a payment in the new group")
    assert membership(world, s0, other) is not None


def test_a_student_who_leaves_and_comes_back_with_a_pause_is_quiet_and_the_cut_pause_is_named_in_the_journal(world):
    from tests.money1b_helpers import add

    ensure_billing(world)
    s0 = world.students[0]
    client = owner(world)
    pay(world, s0, amount=450_000)
    m = mid(world, s0)
    body = {"first_day": (business_today() - timedelta(days=2)).isoformat(), "last_day": (business_today() + timedelta(days=9)).isoformat()}
    assert client.post(f"{API}/org/memberships/{m}/pauses", json=body).status_code == 201
    later = {"first_day": (business_today() + timedelta(days=20)).isoformat(), "last_day": (business_today() + timedelta(days=25)).isoformat()}
    assert client.post(f"{API}/org/memberships/{m}/pauses", json=later).status_code == 201
    quiet(world, "night 0")
    assert client.post(f"{API}/org/groups/{world.group.id}/students/{s0.id}/leave", json={}).status_code == 200
    quiet(world, "the student leaves")
    world.db.expire_all()
    pauses = {p.first_day.isoformat(): p for p in world.db.query(MembershipPause).filter_by(membership_id=m)}
    cut, annulled = pauses[body["first_day"]], pauses[later["first_day"]]
    assert cut.voided_at is None and cut.last_day == business_today() and annulled.voided_at is not None
    lines = {g.snapshot["pause_id"]: g for g in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action.in_(("membership_pause_set", "membership_pause_void")))
             if g.snapshot.get("via") == "leave"}
    assert lines[str(cut.id)].action == "membership_pause_set" and lines[str(cut.id)].snapshot["was"] == [body["first_day"], body["last_day"]] and lines[str(cut.id)].snapshot["last_day"] == business_today().isoformat()
    assert lines[str(annulled.id)].action == "membership_pause_void" and lines[str(annulled.id)].snapshot["was"] == [later["first_day"], later["last_day"]]
    assert lines[str(cut.id)].actor_id == world.owner.id and lines[str(cut.id)].actor_role == "owner"
    add(world, s0, mode="add")
    quiet(world, "the student comes back")


def test_an_opening_balance_replaced_by_the_batch_and_by_the_return_into_a_group_names_the_cancelled_row_in_the_journal(world, fixed_today):
    from tests.test_opening_bulk_pg import D, apply, centre, enrolled, it, live_openings

    fixed_today(date(2026, 11, 20))
    centre(world)
    m = enrolled(world, D(2026, 9, 1), world.students[0])
    quiet(world, "night 0")
    apply(world, [it(world, m, debt_amount=100_000, debt_on="2026-10-31")])
    quiet(world, "an opening balance given by the batch (the import)")
    (first,) = live_openings(world)
    apply(world, [it(world, m, debt_amount=150_000, debt_on="2026-10-31")], reason="Сверили с тетрадью")
    quiet(world, "the batch replaces it")
    world.db.expire_all()
    named = {g.snapshot.get("voided_opening_id") for g in world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="opening_balance_set")}
    assert str(first.id) in named
    (second,) = live_openings(world)
    apply(world, [it(world, m, clear=True)], reason="Долга нет")
    quiet(world, "the batch takes it off")
    voided = {g.snapshot.get("voided_opening_id") for g in world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="opening_balance_void")}
    assert str(second.id) in voided


def test_payouts_in_parts_a_cancelled_payout_and_a_reopened_month_are_quiet_and_every_payout_leaves_a_line_with_its_id_and_sum(world, fixed_today):
    from tests.money1_helpers import reopen_now
    from tests.test_logicA_payroll_pg import g_pay_world, payout, save_sheet

    m, _, _ = g_pay_world(world)
    sheet = save_sheet(world, m)
    quiet(world, "night 0")
    first = payout(world, sheet, 300_000)
    quiet(world, "a payout in parts: the first part")
    second = payout(world, sheet, 175_000)
    quiet(world, "the second part")
    assert owner(world).post(f"{API}/org/payroll/payouts/{second['payout']['id']}/void", json={"reason": "внесли дважды"}).status_code == 200
    quiet(world, "a cancelled payout")
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ставку")
    quiet(world, "a reopened month")
    world.db.expire_all()
    lines = {l.details["payout_id"]: l for l in world.db.query(OrgMoneyAudit).filter(OrgMoneyAudit.org_id == world.org.id, OrgMoneyAudit.action.in_(("payroll_payout_created", "payroll_payout_voided")))}
    created = [l for l in world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="payroll_payout_created")]
    assert sorted(l.details["amount"] for l in created) == [175_000, 300_000] and all(l.actor_id == world.owner.id for l in created)
    voided = world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="payroll_payout_voided").one()
    assert voided.details["payout_id"] == second["payout"]["id"] and voided.details["amount"] == 175_000 and voided.details["reason"] == "внесли дважды"
    assert first["payout"]["id"] in lines
    # the page of the money settings never lists these lines (the sums of the salary are not a setting)
    from src.auth.payments_service import PaymentService

    assert [i for i in PaymentService(world.db).settings_history(str(world.org.id))["items"] if i["action"].startswith("payroll_payout")] == []
