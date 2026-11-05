"""Money-1, stage walk D-2: the LIST of the salaries of a month says what the sheet of the same month says (PostgreSQL).

Stage: November of a teacher is open (the sheet was saved in the middle of it: 45 000; the owner paid an advance of 150 000; the data say 120 834 now).  The sheet: «Месяц открыт: начислено
сейчас 120 834, выплачено 150 000: переплачено 29 166»; the list: «переплачено 105 000» (150 000 - the saved 45 000).  R3 #20: an OPEN month is by the fresh total, a CLOSED one by the saved total; the
row of the list and the payout block of the sheet are ONE function (``PayrollPayoutService.state_in_month``), the header of the list is the sum of the rows."""
from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_p154_money_pg import build_org, five, hold, sheet

pytestmark = needs_pg
MONTH = "2026-10"
LESSON = 25_000                       # 600 000 x 50 % / 12 per student per lesson; five students


def _world(pg_session, clock):
    """October of teacher A: five students paid, one lesson held on 21.10 (5 x 25 000 = 125 000); the sheet is saved on 22.10 and an advance of 300 000 is paid (the owner confirms the overpay);
    then a second lesson is held on 23.10: the data say 250 000 now."""
    w = build_org(pg_session)
    g, st, ms = five(w, clock)
    clock.set("2026-10-22 18:00")
    hold(w, g, "lesson-wed", "2026-10-21", {x: "present" for x in st}, start="10:00", end="11:00")
    calc = sheet(w, MONTH)
    assert calc["total_amount"] == 5 * LESSON
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": MONTH, "teacher_id": str(w.A.id), "expected_fingerprint": calc["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    w.sheet_id = saved.json()["id"]
    url = f"{API}/org/payroll/sheets/{w.sheet_id}/payouts"
    body = {"amount": 300_000, "paid_on": "2026-10-22", "method": "cash"}
    asked = client(w, "owner").post(url, json=body)
    assert asked.status_code == 409 and asked.json()["detail"]["code"] == "OVERPAY_CONFIRM_REQUIRED", asked.text
    paid = client(w, "owner").post(url, json={**body, "confirm_overpay": True, "overpay": asked.json()["detail"]["overpay"]})
    assert paid.status_code == 201, paid.text
    clock.set("2026-10-24 12:00")
    hold(w, g, "lesson-fri", "2026-10-23", {x: "present" for x in st}, start="10:00", end="11:00")
    return w, g, st


def _row_and_sheet(w, who="owner"):
    """The row of teacher A in the list of the month and the payout block of his sheet, as the screens read them."""
    listed = client(w, who).get(f"{API}/org/payroll/months/{MONTH}")
    assert listed.status_code == 200, listed.text
    (row,) = [t for t in listed.json()["teachers"] if t["teacher_id"] == str(w.A.id)]
    full = client(w, who).get(f"{API}/org/payroll/sheets/{w.sheet_id}")
    assert full.status_code == 200, full.text
    assert row["payout"] is not None and full.json()["payout"] is not None
    return row["payout"], full.json()["payout"], listed.json()


def test_d2_an_open_month_the_list_row_is_the_payout_of_the_sheet(pg_session, clock):
    w, g, st = _world(pg_session, clock)
    row, on_sheet, listed = _row_and_sheet(w)
    assert listed["month_open"] is True
    assert row == on_sheet                                                                   # the whole block, field by field
    assert (on_sheet["basis"], on_sheet["state"], on_sheet["saved_total"], on_sheet["fresh_total"], on_sheet["paid"], on_sheet["balance"]) == ("fresh", "overpaid", 125_000, 250_000, 300_000, -50_000)
    assert row["text"].startswith("Месяц открыт: начислено сейчас 250 000") and "50 000" in row["text"]
    assert row["balance"] != 300_000 - 125_000                                               # before: 175 000 «переплачено» by the saved total of the middle of the month


def test_d2_the_header_of_the_list_is_the_sum_of_its_rows_and_so_the_sum_of_the_sheets(pg_session, clock):
    """The front sums ``balance`` over the rows (negative = overpaid): with a second teacher the header is the sum of the sheets' balances, not of the saved totals."""
    w, g, st = _world(pg_session, clock)
    row, on_sheet, listed = _row_and_sheet(w)
    overpaid_rows = sum(-t["payout"]["balance"] for t in listed["teachers"] if t["payout"] and t["payout"]["balance"] < 0)
    assert overpaid_rows == -on_sheet["balance"] == 50_000


def test_d2_a_closed_month_the_row_is_by_the_saved_total_like_the_sheet(pg_session, clock):
    w, g, st = _world(pg_session, clock)
    clock.set("2026-11-02 12:00")                                                            # after the month: the owner saves the sheet again - the month is CLOSED
    calc = sheet(w, MONTH)
    again = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": MONTH, "teacher_id": str(w.A.id), "revision": 1, "expected_fingerprint": calc["source_fingerprint"]})
    assert again.status_code in (200, 201), again.text
    row, on_sheet, listed = _row_and_sheet(w)
    assert listed["month_open"] is False and row == on_sheet
    assert (on_sheet["basis"], on_sheet["state"], on_sheet["saved_total"], on_sheet["paid"], on_sheet["balance"]) == ("saved", "overpaid", 250_000, 300_000, -50_000)
    assert on_sheet["advance_out"] == 50_000                                                  # the advance goes to the next month - one number on both screens


def test_d2_a_closed_month_with_a_drift_after_the_closing_both_say_changed_by_and_stay_by_the_saved_total(pg_session, clock):
    w, g, st = _world(pg_session, clock)
    clock.set("2026-11-02 12:00")
    calc = sheet(w, MONTH)
    assert client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": MONTH, "teacher_id": str(w.A.id), "revision": 1, "expected_fingerprint": calc["source_fingerprint"]}).status_code in (200, 201)
    hold(w, g, "lesson-wed", "2026-10-28", {x: "present" for x in st}, start="10:00", end="11:00")      # data that moved after the closing (written under the rule, as a repair would)
    row, on_sheet, listed = _row_and_sheet(w)
    assert row == on_sheet and on_sheet["basis"] == "saved" and (on_sheet["saved_total"], on_sheet["fresh_total"], on_sheet["changed_by"]) == (250_000, 375_000, 125_000)
    assert on_sheet["balance"] == -50_000                                                    # still by the SAVED sheet; the change is a line of its own


def test_d2_the_list_of_the_month_for_a_reopened_month_is_open_again(pg_session, clock):
    from tests.money1_helpers import reopen_now

    w, g, st = _world(pg_session, clock)
    clock.set("2026-11-02 12:00")
    calc = sheet(w, MONTH)
    assert client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": MONTH, "teacher_id": str(w.A.id), "revision": 1, "expected_fingerprint": calc["source_fingerprint"]}).status_code in (200, 201)
    clock.set("2026-11-02 13:00")
    reopen_now(w.db, w.org.id, w.A.id, MONTH, w.owner.id, "Исправление")
    hold(w, g, "lesson-wed", "2026-10-28", {x: "present" for x in st}, start="10:00", end="11:00")
    row, on_sheet, _ = _row_and_sheet(w)
    assert row == on_sheet and on_sheet["basis"] == "fresh" and on_sheet["fresh_total"] == 375_000 and on_sheet["balance"] == 75_000      # reopened = open: by the fresh total, «к выплате» 75 000
