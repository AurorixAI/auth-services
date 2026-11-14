"""Money-1 G3M Р3-2 (PostgreSQL): the journal of decisions says what the answer says - for ALL six money decisions, in the transaction of the action."""
from datetime import timedelta

import pytest

from src.models.auth import GroupAudit

from tests.pg_world import business_today, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, save_sheet
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import fixed_today  # noqa: F401  (fixture)
from tests.test_payroll_pg import student_group

pytestmark = needs_pg

KINDS = ["payment_writeoff", "payment_writeoff_void", "payment_refund", "payment_refund_void", "payment_void", "payment_replace"]


def _row(client, kind):
    today = business_today()
    rows = [r for r in client.get(f"{API}/org/payments/decisions", params={"from": (today - timedelta(days=1)).isoformat(), "to": (today + timedelta(days=1)).isoformat()}).json()["rows"] if r["kind"] == kind]
    assert len(rows) == 1, (kind, len(rows))
    return rows[0]


def _answer_sentence(response):
    """The payroll sentence of an answer (what the journal keeps); nothing moved - no sentence."""
    effects = response.json()["money_effects"]
    return effects["text"] if effects["payroll"] else None


@pytest.mark.parametrize("kind", KINDS)
def test_r32_the_journal_line_of_every_money_decision_says_the_sentence_of_the_answer(world, kind):
    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    client = owner(world)
    mem2, mem1 = student_group(world, s2), student_group(world, s1)
    if kind.startswith("payment_writeoff"):
        r = client.post(f"{API}/org/memberships/{mem2.id}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
        assert r.status_code == 201, r.text
        if kind == "payment_writeoff_void":
            r = client.post(f"{API}/org/writeoffs/{r.json()['writeoff']['id']}/void", json={"reason": "Ошибка"})
            assert r.status_code == 200, r.text
    elif kind.startswith("payment_refund"):
        pay(world, s1, amount=2_000_000)
        r = client.post(f"{API}/org/memberships/{mem1.id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "Выбыл на неделю"})
        assert r.status_code == 201, r.text
        if kind == "payment_refund_void":
            r = client.post(f"{API}/org/refunds/{r.json()['refund']['id']}/void", json={"reason": "Ошибка"})
            assert r.status_code == 200, r.text
    else:
        paid = pay(world, s1, amount=500_000)["payment"]
        url = f"{API}/org/payments/{paid['id']}"
        r = (client.post(f"{url}/void", json={"reason": "ошибка"}) if kind == "payment_void" else
             client.post(f"{url}/replace", json={"amount": 400_000, "paid_on": paid["paid_on"], "method": "cash", "reason": "Опечатка"}))
        assert r.status_code in (200, 201), r.text
    expected = _answer_sentence(r)
    line = _row(client, kind)
    assert line["consequence"] == expected
    if kind in ("payment_writeoff", "payment_writeoff_void"):
        assert expected and "арплата за" in expected                      # the forgiveness really moves a saved sheet: the journal is not empty by accident


def test_r32_an_administrator_with_the_right_reads_the_forgiveness_sentence_without_the_sums(world):
    from tests.ops_helpers import grant_money_right
    from tests.pg_world import make_client

    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    grant_money_right(world)
    r = owner(world).post(f"{API}/org/memberships/{student_group(world, s2).id}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    assert r.status_code == 201, r.text
    shown = f"{abs(r.json()['money_effects']['payroll'][0]['changedBy']):,}".replace(",", " ")
    assert shown in _row(owner(world), "payment_writeoff")["consequence"]
    assert "арплата за" in _row(make_client(world, user=world.admin), "payment_writeoff")["consequence"] and shown not in _row(make_client(world, user=world.admin), "payment_writeoff")["consequence"]


def test_r32_a_refused_decision_leaves_no_line_in_the_journal(world):
    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    before = world.db.query(GroupAudit).count()
    r = owner(world).post(f"{API}/org/memberships/{student_group(world, s2).id}/writeoffs",
                          json={"all": True, "reason": "Договорённость с родителем", "expected_debt": 1})
    assert r.status_code == 409, r.text                                    # the debt the owner saw is not the debt now: nothing is written
    world.db.expire_all()
    assert world.db.query(GroupAudit).count() == before
