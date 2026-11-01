"""Wave B P4 (money): MN-32 (a payout above the balance needs a confirmation), MN-42 / MN-51 (sums are strict integers), MN-50 (a carry-over copy cannot be
voided), MN-06 (no lessons package and no ``since`` is an empty answer), MN-25 (the accounting start is a Tashkent day), MN-35 (a newer rate always wins),
DM-006 (leaving locks the student without blocking rows that point at him)."""
import uuid
from datetime import date, datetime, timedelta

import pytest

from src.auth import payment_status as ps
from src.auth.payroll_payouts import OverpayConfirmRequired, PayrollPayoutService
from src.auth.payroll_schemas import PayoutCreate
from src.auth.payroll_service import PayrollService
from src.models.auth import PayrollPayout, PayrollSheet

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, save_sheet
from tests.test_payment_carry_over_pg import engine, run_in_thread  # noqa: F401  (engine = fixture)
from tests.test_pay2_api_pg import fixed_today  # noqa: F401  (fixture)

pytestmark = needs_pg


def _today():
    return ps.business_today()


def post_payout(w, sheet, amount, **extra):
    body = {"amount": amount, "paid_on": _today().isoformat(), "method": "cash", **extra}
    return owner(w).post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json=body)


def live_payouts(w, sheet):
    w.db.expire_all()
    return sorted(int(p.amount) for p in w.db.query(PayrollPayout).filter_by(sheet_id=uuid.UUID(sheet["id"]), voided_at=None))


# ---- MN-32 --------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fixed_today")
def test_mn32_payout_above_the_balance_is_409_until_the_advance_is_confirmed(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)                       # 475 000 to pay
    r = post_payout(world, sheet, 500_000)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "OVERPAY_CONFIRM_REQUIRED" and detail["overpay"] == 25_000 and detail["balance"] == 475_000
    assert detail["message"] == "Выплата больше остатка на 25 000 сум"
    assert live_payouts(world, sheet) == []            # nothing was written by the refusal
    assert post_payout(world, sheet, 500_000, confirm_overpay=False).status_code == 409
    done = post_payout(world, sheet, 500_000, confirm_overpay=True)
    assert done.status_code == 201 and done.json()["state"]["state"] == "overpaid" and done.json()["state"]["balance"] == -25_000
    assert live_payouts(world, sheet) == [500_000]


@pytest.mark.usefixtures("fixed_today")
def test_mn32_the_balance_is_counted_after_the_earlier_payouts_and_a_voided_one_frees_it(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    first = post_payout(world, sheet, 300_000)
    assert first.status_code == 201                    # below the balance: no question
    r = post_payout(world, sheet, 200_000)             # 175 000 are left
    assert r.status_code == 409 and r.json()["detail"]["overpay"] == 25_000 and r.json()["detail"]["balance"] == 175_000
    assert post_payout(world, sheet, 175_000).status_code == 201            # exactly the rest is not an advance
    r = post_payout(world, sheet, 1)                   # everything is paid: even 1 sum is above the balance (0)
    assert r.status_code == 409 and r.json()["detail"]["overpay"] == 1 and r.json()["detail"]["balance"] == 0
    voided = owner(world).post(f"{API}/org/payroll/payouts/{first.json()['payout']['id']}/void", json={"reason": "внесли дважды"})
    assert voided.status_code == 200
    assert post_payout(world, sheet, 300_000).status_code == 201            # the voided payout is not counted: the balance is back
    assert live_payouts(world, sheet) == [175_000, 300_000]


@pytest.mark.usefixtures("fixed_today")
def test_mn32_a_replayed_key_is_a_duplicate_not_a_question_and_an_unsaved_sheet_stays_409_not_saved(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    key = str(uuid.uuid4())
    base = {"amount": 600_000, "paid_on": _today().isoformat(), "method": "cash", "confirm_overpay": True}
    c = owner(world)
    first = c.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json=base, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    again = c.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json={**base, "confirm_overpay": False}, headers={"Idempotency-Key": key})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "DUPLICATE_PAYOUT"      # the retry of the sent request, not a new question
    missing = c.post(f"{API}/org/payroll/sheets/{uuid.uuid4()}/payouts", json={**base, "confirm_overpay": False})
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "SHEET_NOT_SAVED"


@pytest.mark.usefixtures("fixed_today")
def test_mn32_two_payouts_in_parallel_cannot_both_slip_under_the_balance(world, engine):
    """300 000 + 300 000 against 475 000: the sheet lock serialises them, the second sees the first one and must ask for the confirmation."""
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    org, actor, sid = str(world.org.id), str(world.owner.id), uuid.UUID(sheet["id"])

    def pay(s):
        row = s.get(PayrollSheet, sid)
        body = PayoutCreate(amount=300_000, paid_on=_today(), method="cash")
        return PayrollPayoutService(s).create(org, sid, actor, body, uuid.uuid4(), lambda r: PayrollService(s)._fresh_total(org, r))

    threads = [run_in_thread(engine, pay) for _ in range(2)]
    for t, _ in threads:
        t.join(timeout=60)
    outcomes = [o for _, o in threads]
    done = [o for o in outcomes if "value" in o]
    refused = [o["error"] for o in outcomes if "error" in o]
    assert len(done) == 1 and len(refused) == 1, outcomes
    assert isinstance(refused[0], OverpayConfirmRequired) and refused[0].extra["overpay"] == 125_000 and refused[0].extra["balance"] == 175_000
    assert live_payouts(world, sheet) == [300_000]


# ---- MN-42 / MN-51 ------------------------------------------------------------------------------------------------------------------------

BAD_SUMS = [True, False, "100", "1e3", " 100", 1.5, 100.0, [100], {"v": 100}]
UUID0 = "00000000-0000-4000-8000-000000000001"


def _is_type_error(response):
    """A pydantic 422 (a LIST of errors that names ``amount``), not the service's own ``AMOUNT_RANGE`` (a dict): the value never reached the service."""
    detail = response.json().get("detail")
    return response.status_code == 422 and isinstance(detail, list) and any("amount" in (e.get("loc") or ()) for e in detail)


@pytest.mark.parametrize("bad", BAD_SUMS, ids=[repr(b) for b in BAD_SUMS])
@pytest.mark.parametrize("path,extra", [
    (f"/org/memberships/{UUID0}/payments", {"paid_on": "2026-10-01", "method": "cash"}),
    (f"/org/payments/{UUID0}/replace", {"paid_on": "2026-10-01", "method": "cash"}),
    (f"/org/memberships/{UUID0}/refunds", {"method": "cash", "reason": "ошибка"}),
    (f"/org/memberships/{UUID0}/writeoffs", {"reason": "договорённость"}),
    (f"/org/payroll/sheets/{UUID0}/payouts", {"paid_on": "2026-10-01", "method": "cash"}),
], ids=["payment", "replace", "refund", "writeoff", "payout"])
def test_mn42_mn51_an_amount_that_is_not_an_integer_is_422_on_every_money_endpoint(world, path, extra, bad):
    r = owner(world).post(f"{API}{path}", json={"amount": bad, **extra})
    assert _is_type_error(r), (bad, r.status_code, r.text[:200])


def test_mn42_an_integer_still_reaches_the_service(world):
    """The control of the test above: a real integer is NOT a type error (the unknown membership is a 404, the unknown sheet a 409)."""
    c = owner(world)
    r = c.post(f"{API}/org/memberships/{UUID0}/payments", json={"amount": 100, "paid_on": "2026-10-01", "method": "cash"})
    assert r.status_code in (404, 409) and not _is_type_error(r), r.text[:200]
    r = c.post(f"{API}/org/payroll/sheets/{UUID0}/payouts", json={"amount": 100, "paid_on": "2026-10-01", "method": "cash"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_NOT_SAVED"
