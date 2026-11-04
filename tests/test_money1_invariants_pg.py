"""Money-1 invariants I-1 .. I-7 (the nightly reconciliation checks I-1 .. I-4 in ``test_money1_reconcile_pg.py``; here the real centre, with numbers) (PostgreSQL)."""
from datetime import date

import pytest

from src.auth import money_reconcile as mr
from src.auth.payments_schemas import PaymentCreated, PaymentVoided, RefundCreated, RefundVoided, WriteoffCreated, WriteoffVoided
from src.models.auth import GroupAttendance

from tests.pg_world import ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, save_sheet
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)
from tests.test_payroll_pg import lesson as held

pytestmark = needs_pg


def test_i1_the_account_of_every_membership_adds_up_after_payments_a_writeoff_and_a_refund(world):
    """charged - paid - written_off + refunded == debt - advance on a centre that has all four kinds of records; the reconciliation finds nothing."""
    ensure_billing(world)
    s0, s1, s2 = world.students
    pay(world, s0, amount=450_000)                                                                  # exactly one month
    pay(world, s1, amount=2_000_000)                                                                # an advance
    from tests.test_payments_service_pg import mid

    r = owner(world).post(f"{API}/org/memberships/{mid(world, s1)}/refunds", json={"amount": 500_000, "method": "cash", "reason": "Выбыл на месяц"})
    assert r.status_code == 201, r.text
    w = owner(world).post(f"{API}/org/memberships/{mid(world, s2)}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    assert w.status_code in (201, 409, 422), w.text                                                 # s2 may have no debt in this world: the identity must hold anyway
    assert [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-1"] == []


def test_i3_a_held_lesson_where_everybody_is_absent_is_paid_and_a_lesson_cancelled_by_the_centre_pays_nothing(world):
    """Numbers: 50 %, price 600 000, divisor 12 -> 25 000 per student per lesson.  S1 paid the whole month (+25 000 for the all-absent lesson of the 29th); S2 only through the 15th (unpaid)."""
    m, s1, s2 = g_pay_world(world)
    base = save_sheet(world, m)["total_amount"]
    assert base == 475_000
    held(world, m.d(29), {s1: "absent", s2: "absent"})                                              # held, nobody came: the lesson is HELD (rule 4.3)
    r = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={"teacher_ids": [str(world.teacher.id)]})
    assert r.json()["sheets"][0]["total_amount"] == 500_000
    held(world, m.d(30), {}, status="cancelled")                                                    # cancelled by the centre: no marks, nobody is paid
    r = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={"teacher_ids": [str(world.teacher.id)]})
    assert r.json()["sheets"][0]["total_amount"] == 500_000


def test_i5_every_money_answer_has_the_place_for_the_consequences():
    """The answers of a payment, a write-off and a refund (and their cancellation) carry ``money_effects`` - the front shows it in the confirmation."""
    for model in (PaymentCreated, PaymentVoided, WriteoffCreated, WriteoffVoided, RefundCreated, RefundVoided):
        assert "money_effects" in model.model_fields, model


def test_i5_a_forgiven_debt_is_paid_to_the_teacher_and_the_answer_says_how_much_ЗПД5(world):
    """ЗПД-5: S2 owes the second half of October (300 000); forgiving it makes the 5 lessons after the 15th paid: +5 x 25 000 = +125 000 on the saved 475 000."""
    from tests.test_payroll_pg import student_group

    m, s1, s2 = g_pay_world(world)
    sheet_ = save_sheet(world, m)
    mem2 = student_group(world, s2)
    r = owner(world).post(f"{API}/org/memberships/{mem2.id}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    assert r.status_code == 201, r.text
    (line,) = [p for p in r.json()["money_effects"]["payroll"] if p["month"] == m.label]
    assert (line["savedTotal"], line["changedBy"]) == (475_000, 125_000) and "+125 000" in r.json()["money_effects"]["text"]
    voided = owner(world).post(f"{API}/org/writeoffs/{r.json()['writeoff']['id']}/void", json={"reason": "Ошибка"})
    assert voided.status_code == 200
    (back,) = voided.json()["money_effects"]["payroll"]                                              # R3 #21: the effect of THIS action - the debt is back, the sheet moves down by what the forgiveness added
    assert (back["freshBefore"], back["freshAfter"], back["changedBy"], back["driftAfter"]) == (600_000, 475_000, -125_000, 0)
