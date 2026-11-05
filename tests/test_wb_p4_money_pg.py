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
    assert detail["message"].startswith("Выплата больше остатка на 25 000 сум") and "обновите страницу" in detail["message"]    # an OLD tab (no flag) is told what to do (R3 9)
    assert live_payouts(world, sheet) == []            # nothing was written by the refusal
    assert post_payout(world, sheet, 500_000, confirm_overpay=False).status_code == 409
    assert post_payout(world, sheet, 500_000, confirm_overpay=True).status_code == 409, "the flag alone is no answer to «on how much»: the sum that was shown must come with it (R3 7)"
    done = post_payout(world, sheet, 500_000, confirm_overpay=True, overpay=25_000)
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
    base = {"amount": 600_000, "paid_on": _today().isoformat(), "method": "cash", "confirm_overpay": True, "overpay": 125_000}
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


# ---- R3 2: every other money input is strict too (prices, openings, settlements, the individual price of an enrolment) ----------------------------------------------

M0 = "00000000-0000-4000-8000-000000000002"
PRICE_INPUTS = [
    ("put", f"/org/groups/{UUID0}/price", lambda bad: {"amount": bad, "valid_from": "2026-10-01"}, "amount"),
    ("put", f"/org/memberships/{M0}/price-override", lambda bad: {"amount": bad, "valid_from": "2026-10-01", "reason": "договорённость"}, "amount"),
    ("put", f"/org/memberships/{M0}/opening-balance", lambda bad: {"debt_amount": bad, "debt_on": "2026-09-01", "reason": "импорт долга"}, "debt_amount"),
    ("post", "/org/opening-balances/batch/preview", lambda bad: {"items": [{"membership_id": M0, "version": "v", "debt_amount": bad, "debt_on": "2026-09-01"}]}, "debt_amount"),
    ("post", "/org/opening-balances/batch/preview", lambda bad: {"items": [{"membership_id": M0, "version": "v", "lessons_left": bad}]}, "lessons_left"),
    ("post", f"/org/groups/{UUID0}/students/{M0}", lambda bad: {"price_override": {"amount": bad, "reason": "скидка семье"}}, "amount"),
    ("post", f"/org/groups/{UUID0}/students/{M0}/leave", lambda bad: {"settle": [{"membership_id": M0, "action": "refund", "expected_refundable": bad, "method": "cash"}]}, "expected_refundable"),
    ("post", f"/org/groups/{UUID0}/students/{M0}/leave", lambda bad: {"settle": [{"membership_id": M0, "action": "writeoff", "expected_debt": bad, "reason": "договорились"}]}, "expected_debt"),
]


def _names_field(response, field):
    detail = response.json().get("detail")
    return response.status_code == 422 and isinstance(detail, list) and any(field in (e.get("loc") or ()) for e in detail)


@pytest.mark.parametrize("bad", [True, False, "500000", "1e3", 1.5, 500000.0], ids=repr)
@pytest.mark.parametrize("method,path,body,field", PRICE_INPUTS, ids=["group-price", "price-override", "opening", "batch-debt", "batch-lessons", "enrol-price", "settle-refund", "settle-writeoff"])
def test_r3_2_every_money_input_refuses_what_is_not_an_integer(world, method, path, body, field, bad):
    r = getattr(owner(world), method)(f"{API}{path}", json=body(bad))
    assert _names_field(r, field), (path, bad, r.status_code, r.text[:200])


def test_r3_2_a_real_integer_is_still_taken_by_every_one_of_them(world):
    """The control: with an integer the request gets PAST the validation (404 / 409 / 422 of the domain, never the type error of the field)."""
    for method, path, body, field in PRICE_INPUTS:
        r = getattr(owner(world), method)(f"{API}{path}", json=body(500000 if field != "lessons_left" else 3))
        assert not _names_field(r, field), (path, r.status_code, r.text[:200])


def test_r3_2_the_excel_import_of_openings_still_builds_integer_items(world):
    """The Excel parsers go through ``_parse_money -> int`` (a ``float`` in a cell is parsed and cast): the items they build are accepted by the strict model."""
    from src.auth.import_service import _parse_money
    from src.auth.openings_schemas import OpeningBatchItem
    from src.auth.payments_schemas import OpeningIn

    assert _parse_money("1 500 000,0") == 1_500_000 and isinstance(_parse_money("250000"), int)
    item = OpeningBatchItem(membership_id=M0, version="v", debt_amount=_parse_money("250 000"), debt_on=date(2026, 9, 1), lessons_left=3)
    assert item.debt_amount == 250_000
    assert OpeningIn(debt_amount=_parse_money("100000"), debt_on=date(2026, 9, 1)).debt_amount == 100_000


# ---- R3 7: ``confirm_overpay`` is a real boolean and belongs to the sum that was shown --------------------------------------------------------------------------------


@pytest.mark.usefixtures("fixed_today")
def test_r3_7_the_flag_is_strict_and_the_confirmation_is_for_the_sum_the_owner_saw(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)                       # 475 000
    for bad in ("yes", "1", 1, "true"):
        r = post_payout(world, sheet, 500_000, confirm_overpay=bad, overpay=25_000)
        assert r.status_code == 422, (bad, r.text)
    asked = post_payout(world, sheet, 500_000)
    assert asked.status_code == 409 and asked.json()["detail"]["overpay"] == 25_000
    other_tab = post_payout(world, sheet, 10_000)       # meanwhile another tab pays 10 000: the real overpay of the same 500 000 is now 35 000
    assert other_tab.status_code == 201
    stale = post_payout(world, sheet, 500_000, confirm_overpay=True, overpay=25_000)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "OVERPAY_CONFIRM_REQUIRED" and stale.json()["detail"]["overpay"] == 35_000
    assert live_payouts(world, sheet) == [10_000], "the old confirmation wrote nothing"
    done = post_payout(world, sheet, 500_000, confirm_overpay=True, overpay=35_000)
    assert done.status_code == 201 and live_payouts(world, sheet) == [10_000, 500_000]
    exact = post_payout(world, sheet, 1, confirm_overpay=True, overpay=1)           # a confirmation of the exact excess is accepted whatever the question was
    assert exact.status_code == 201


# ---- R3 3: the advance is carried to the next month of the same teacher -----------------------------------------------------------------------------------------------


def _sheet(world, first, total):
    """A SAVED sheet of the teacher for a month, written directly (the money of the chain is what is under test, not the calculation)."""
    from src.models.auth import PayrollSheet

    row = PayrollSheet(org_id=world.org.id, teacher_id=world.teacher.id, period_month=first, percent_inputs={}, divisor_default=12, lines=[], total_amount=total,
                       source_fingerprint="x", revision=1, created_by=world.owner.id, updated_by=world.owner.id)
    world.db.add(row)
    world.db.commit()
    return row


def _pay(world, sheet, amount, overpay=None):
    body = PayoutCreate(amount=amount, paid_on=_today(), method="cash", confirm_overpay=overpay is not None, overpay=overpay)
    return PayrollPayoutService(world.db).create(str(world.org.id), sheet.id, str(world.owner.id), body, uuid.uuid4(), lambda s: None)


def _state(world, sheet):
    world.db.expire_all()
    return PayrollPayoutService(world.db).state_for(str(world.org.id), world.db.get(type(sheet), sheet.id), None)


AUG, SEP, OCT, NOV = date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)


def test_r3_3_an_advance_is_taken_off_the_next_month_and_what_does_not_fit_goes_on(world):
    """Aug 475 000, paid 600 000 (an advance of 125 000) -> Sep 100 000: covered whole (used 100 000, target 0), 25 000 go on -> Oct 300 000: target 275 000."""
    aug, sep, octo = _sheet(world, AUG, 475_000), _sheet(world, SEP, 100_000), _sheet(world, OCT, 300_000)
    first = _pay(world, aug, 600_000, overpay=125_000)
    st = _state(world, aug)
    assert (st.carry_in, st.carry_used, st.target, st.balance, st.advance_out, st.state) == (0, 0, 475_000, -125_000, 125_000, "overpaid")
    assert st.text == "Выплачено больше на 125 000 сум — вычтем из следующей выплаты", "the text of a month without a carry is the old one"
    s = _state(world, sep)
    assert (s.carry_in, s.carry_used, s.target, s.balance, s.advance_out, s.state) == (125_000, 100_000, 0, 0, 25_000, "settled")
    assert s.text == "Закрыто выплатой прошлого месяца: 100 000 сум"
    o = _state(world, octo)
    assert (o.carry_in, o.carry_used, o.target, o.balance, o.advance_out, o.state) == (25_000, 25_000, 275_000, 275_000, 0, "unpaid")
    assert o.text == "Не выплачено"
    # the sheet of the past month is not touched
    world.db.expire_all()
    assert int(world.db.get(type(aug), aug.id).total_amount) == 475_000 and world.db.get(type(aug), aug.id).revision == 1
    # the owner pays exactly what is left of October: no question (the advance is already off), nothing is carried further
    done = _pay(world, octo, 275_000)
    assert done.state.state == "settled" and done.state.advance_out == 0
    # voiding the advance payout takes the carry away from every later month at once
    PayrollPayoutService(world.db).void(str(world.org.id), uuid.UUID(first.payout.id), str(world.owner.id), "ошибка", lambda s: None)
    assert _state(world, aug).target == 475_000 and _state(world, aug).balance == 475_000 - 0
    assert (_state(world, sep).carry_in, _state(world, sep).target) == (0, 100_000)
    assert (_state(world, octo).carry_in, _state(world, octo).balance) == (0, 300_000 - 275_000), "October: the 275 000 paid are now against the full 300 000"


def test_r3_3_three_months_in_a_row_with_a_gap_and_a_second_teacher(world):
    """Aug -> Sep -> Nov (October has no sheet): the advance passes through the gap; another teacher's months are separate."""
    aug, sep, nov = _sheet(world, AUG, 100_000), _sheet(world, SEP, 40_000), _sheet(world, NOV, 500_000)
    _pay(world, aug, 150_000, overpay=50_000)
    assert [(_state(world, x).carry_in, _state(world, x).carry_used, _state(world, x).advance_out) for x in (aug, sep, nov)] == [(0, 0, 50_000), (50_000, 40_000, 10_000), (10_000, 10_000, 0)]
    other = PayrollPayoutService(world.db).carry_ins(str(world.org.id), [world.admin.id, world.teacher.id], NOV)
    assert other == {world.admin.id: 0, world.teacher.id: 10_000}


def test_r3_3_the_question_of_an_advance_counts_the_balance_after_the_carry(world):
    aug, sep = _sheet(world, AUG, 100_000), _sheet(world, SEP, 100_000)
    _pay(world, aug, 130_000, overpay=30_000)
    with pytest.raises(OverpayConfirmRequired) as asked:
        _pay(world, sep, 80_000)                        # September owes 70 000 after the advance: 80 000 is 10 000 too much
    assert asked.value.extra == {"overpay": 10_000, "balance": 70_000}
    assert _pay(world, sep, 70_000).state.state == "settled"


def test_r3_3_the_month_overview_and_the_excel_sheet_tell_the_advance(world):
    from src.auth import payroll_export

    aug, sep = _sheet(world, AUG, 100_000), _sheet(world, SEP, 100_000)
    _pay(world, aug, 130_000, overpay=30_000)
    payout = _state(world, sep).model_dump()
    assert payout["carry_used"] == 30_000
    assert payroll_export.advance_lines(payout) == ["Выплачено больше в прошлом месяце: 30 000 сум — вычтено, «К выплате» уменьшена на эту сумму"]
    assert payroll_export._balance(payout, 100_000) == (100_000, 0, 70_000, "К выплате"), "«Заработано» stays the whole total, «К выплате» is after the advance"
    assert payroll_export.advance_lines(_state(world, aug).model_dump()) == ["Вычтем из следующей выплаты: 30 000 сум"]
    assert payroll_export.advance_lines(None) == []


def test_r3_3_the_api_and_the_excel_file_of_the_next_month_carry_the_advance_line(world):
    import io

    from openpyxl import load_workbook

    aug, sep = _sheet(world, AUG, 100_000), _sheet(world, SEP, 100_000)
    sep.lines = [{"line_key": "m", "membership_id": str(uuid.uuid4()), "student_id": str(uuid.uuid4()), "student_name": "Ученик", "group_id": str(world.group.id),
                  "group_name": "Группа", "lesson_type": "group", "fee_overridden": True, "amount": 100_000, "counted": 0, "unpaid": 0, "pending_excused": 0, "written_off": 0, "undecided": 0, "no_pay": 0, "marks": []}]       # a manual price has no «fresh» total: the saved one is the target
    world.db.commit()
    _pay(world, aug, 130_000, overpay=30_000)
    c = owner(world)
    got = c.get(f"{API}/org/payroll/sheets/{sep.id}")
    assert got.status_code == 200, got.text
    payout = got.json()["payout"]
    assert (payout["carry_in"], payout["carry_used"], payout["target"], payout["balance"]) == (30_000, 30_000, 70_000, 70_000)
    xlsx = c.get(f"{API}/org/payroll/sheets/{sep.id}/export.xlsx")
    assert xlsx.status_code == 200, xlsx.text[:200]
    cells = [str(cell.value) for sheet in load_workbook(io.BytesIO(xlsx.content)).worksheets for row in sheet.iter_rows() for cell in row if cell.value]
    assert any("Выплачено больше в прошлом месяце: 30 000 сум" in value for value in cells), cells[:40]
