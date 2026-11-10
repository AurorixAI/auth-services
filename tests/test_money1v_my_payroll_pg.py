"""Money-1 pack V8 (PostgreSQL): «Моя зарплата» - the teacher sees HIS month: lessons counted, the sum, paid, the rest.  No prices, no students, no debts (owner's decision 05.11).

World of ``test_money1v_payroll_pg``: October closed at 250 000 (two lessons, five students, 25 000 each), 300 000 paid: 50 000 forward."""
import re

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_money1v_payroll_pg import _closed
from tests.test_p154_money_pg import hold, sheet

pytestmark = needs_pg
URL = f"{API}/org/me/payroll"
FORBIDDEN_KEYS = re.compile(r"price|student|debt|name|fee|group|unpaid|percent|rate", re.I)


def keys_of(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            keys_of(v, found)
    elif isinstance(value, list):
        for v in value:
            keys_of(v, found)
    return found


def test_v8_a_closed_month_is_the_saved_sheet_and_the_answer_has_no_prices_students_or_debts(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    r = client(w, "A").get(URL, params={"month": "2026-10"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["state"], got["preliminary"], got["lessons_counted"], got["total"], got["paid"], got["balance"]) == ("saved", False, 2, 250_000, 300_000, -50_000)
    assert got["text"] == "Выплачено вперёд 50 000 сум"
    assert not [k for k in keys_of(got) if FORBIDDEN_KEYS.search(k)], keys_of(got)          # LESSONS 95: the answer carries only what the screen shows


def test_v8_the_teacher_sees_the_saved_sum_even_when_the_data_moved_after_the_closing(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    hold(w, g, "lesson-wed", "2026-10-28", {x: "present" for x in st}, start="10:00", end="11:00")      # the fresh total is 375 000 now; the sheet says 250 000
    got = client(w, "A").get(URL, params={"month": "2026-10"}).json()
    assert got["total"] == 250_000 and got["lessons_counted"] == 3 and got["state"] == "saved"        # money: the saved sheet is the truth; lessons: the number of the owner's list (R3 Д6, held lessons)


def test_v8_the_current_month_is_preliminary_and_a_finished_month_without_a_saved_sheet_says_so(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    hold(w, g, "lesson-mon", "2026-11-02", {x: "present" for x in st}, start="10:00", end="11:00")
    current = client(w, "A").get(URL, params={"month": "2026-11"}).json()
    assert (current["state"], current["preliminary"], current["lessons_counted"], current["total"]) == ("open_preliminary", True, 1, 125_000)
    assert current["text"].startswith("Предварительно: сумма может измениться")
    nothing = client(w, "B").get(URL, params={"month": "2026-10"}).json()                              # the other teacher: HIS month, not A's
    assert nothing["state"] == "not_saved" and nothing["total"] is None and nothing["text"] == "Зарплата за месяц ещё не сохранена"
    assert client(w, "A").get(URL, params={"month": "2027-03"}).status_code == 422                       # a month that has not come


def test_v8_the_list_of_months_and_the_rights(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    hold(w, g, "lesson-mon", "2026-11-02", {x: "present" for x in st}, start="10:00", end="11:00")
    months = client(w, "A").get(f"{URL}/months").json()["items"]
    assert [(m["month"], m["state"]) for m in months] == [("2026-11", "open_preliminary"), ("2026-10", "saved")]
    for who in ("owner", "admin"):
        assert client(w, who).get(URL, params={"month": "2026-10"}).status_code == 403, who
        assert client(w, who).get(f"{URL}/months").status_code == 403, who


# ---- R3 Д1 / Д2: «Моя зарплата» = the numbers of the owner (LESSONS 104), four scenarios --------------------------------------------------------


def owner_row(w, month):
    """The row of teacher A in the list of the month of the owner: the payout block (the function of the list) and the sheet."""
    listed = client(w, "owner").get(f"{API}/org/payroll/months/{month}").json()
    (row,) = [t for t in listed["teachers"] if t["teacher_id"] == str(w.A.id)]
    return row


def same_numbers(w, month):
    mine = client(w, "A").get(URL, params={"month": month}).json()
    payout = owner_row(w, month)["payout"]
    assert payout is not None, month
    expected_total = payout["fresh_total"] if payout["basis"] == "fresh" and payout["fresh_total"] is not None else payout["saved_total"]
    assert (mine["total"], mine["paid"], mine["balance"]) == (expected_total, payout["paid"], payout["balance"]), (mine, payout)
    return mine, payout


def _world_with_an_unpaid_student(pg_session, clock):
    """October of teacher A as ``test_money1_payout_list_pg._world`` but with a sixth student who has NOT paid: his lesson of 21.10 is «не оплачено»; the sheet is saved on 22.10, the owner ticks
    «начислить» on his line (+25 000 for the lesson), an advance of 300 000 is paid, a second lesson is held on 23.10 (the data move on)."""
    from datetime import datetime

    from tests.ops_helpers import new_student
    from tests.test_money1_payout_list_pg import LESSON
    from tests.test_p154_money_pg import build_org, five

    w = build_org(pg_session)
    g, st, ms = five(w, clock)
    u = new_student(w.db, w.org, w.ids, "x9", "Без", "Оплаты", g, joined=datetime(2026, 10, 6, 6, 0))
    w.db.commit()
    clock.set("2026-10-22 18:00")
    hold(w, g, "lesson-wed", "2026-10-21", {x: "present" for x in [*st, u]}, start="10:00", end="11:00")
    calc = sheet(w, "2026-10")
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "expected_fingerprint": calc["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    line = [x for x in saved.json()["lines"] if x.get("unpaid")][0]
    ticked = client(w, "owner").patch(f"{API}/org/payroll/sheets/{saved.json()['id']}", json={"revision": saved.json()["revision"], "line_overrides": [{"membership_id": line["membership_id"], "include_unpaid": True}]})
    assert ticked.status_code == 200, ticked.text
    w.sheet_id = saved.json()["id"]
    url = f"{API}/org/payroll/sheets/{w.sheet_id}/payouts"
    body = {"amount": 300_000, "paid_on": "2026-10-22", "method": "cash"}
    asked = client(w, "owner").post(url, json=body)
    if asked.status_code == 409:
        asked = client(w, "owner").post(url, json={**body, "confirm_overpay": True, "overpay": asked.json()["detail"]["overpay"]})
    assert asked.status_code == 201, asked.text
    clock.set("2026-10-24 12:00")
    hold(w, g, "lesson-fri", "2026-10-23", {x: "present" for x in st}, start="10:00", end="11:00")
    return w, g, st, u, LESSON


def test_d1_the_current_month_with_a_tick_is_the_number_of_the_owner(pg_session, clock):
    w, g, st, u, lesson = _world_with_an_unpaid_student(pg_session, clock)
    mine, payout = same_numbers(w, "2026-10")
    assert mine["preliminary"] is True and payout["basis"] == "fresh"
    assert mine["total"] % lesson == 0 and mine["total"] > 5 * lesson * 2         # the ticked lesson of the sixth student is in: more than the two lessons of the five (the old code lost it)


def test_d2_a_month_saved_inside_it_after_its_end_shows_the_payouts_not_zero(pg_session, clock):
    w, g, st, u, lesson = _world_with_an_unpaid_student(pg_session, clock)
    clock.set("2026-11-02 12:00")                                                       # October is over, its sheet was saved INSIDE it: the month is open, not closed
    mine, payout = same_numbers(w, "2026-10")
    assert mine["state"] == "open_preliminary" and mine["paid"] == 300_000 and mine["text"].startswith("Предварительно")     # the old code: not_saved, paid 0


def test_d2_a_reopened_month_and_a_closed_month_are_the_numbers_of_the_owner(pg_session, clock):
    from tests.money1_helpers import reopen_now

    w, g, st, u, lesson = _world_with_an_unpaid_student(pg_session, clock)
    clock.set("2026-11-02 12:00")
    calc = sheet(w, "2026-10")
    assert client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "revision": 2, "expected_fingerprint": calc["source_fingerprint"]}).status_code in (200, 201)
    closed_mine, closed_payout = same_numbers(w, "2026-10")
    assert closed_mine["state"] == "saved" and closed_payout["basis"] == "saved"
    clock.set("2026-11-02 13:00")
    reopen_now(w.db, w.org.id, w.A.id, "2026-10", w.owner.id, "Исправление")
    mine, payout = same_numbers(w, "2026-10")
    assert mine["state"] == "open_preliminary" and payout["basis"] == "fresh"


def test_r3_d6_lessons_in_my_payroll_are_the_lessons_of_the_owners_list_not_the_paid_ones(pg_session, clock):
    """Stage walk: «Засчитано уроков 3» here, «5 уроков» in the owner's list: one number, one meaning - the lessons the teacher HELD (rule 104)."""
    w, g, st = _closed(pg_session, clock)
    hold(w, g, "lesson-mon", "2026-11-02", {x: "present" for x in st}, start="10:00", end="11:00")
    hold(w, g, "lesson-wed", "2026-11-04", {x: "absent" for x in st}, start="10:00", end="11:00")          # held, nobody came: pays nothing, still a lesson
    listed = client(w, "owner").get(f"{API}/org/payroll/months/2026-11").json()["teachers"]
    (row,) = [t for t in listed if t["teacher_id"] == str(w.A.id)]
    mine = client(w, "A").get(URL, params={"month": "2026-11"}).json()
    assert row["lessons_count"] == mine["lessons_counted"] == 2                                              # the old code: 1 here
    months = {m["month"]: m for m in client(w, "A").get(f"{URL}/months").json()["items"]}
    assert months["2026-11"]["lessons_counted"] == 2 and months["2026-10"]["lessons_counted"] == 2
    october = [t for t in client(w, "owner").get(f"{API}/org/payroll/months/2026-10").json()["teachers"] if t["teacher_id"] == str(w.A.id)][0]
    assert october["lessons_count"] == months["2026-10"]["lessons_counted"]
