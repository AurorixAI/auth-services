"""Money-1 pack V8 (PostgreSQL): «Моя зарплата» - the teacher sees HIS month: lessons counted, the sum, paid, the rest.  No prices, no students, no debts (owner's decision 05.11).

World of ``test_money1v_payroll_pg``: October closed at 250 000 (two lessons, five students, 25 000 each), 300 000 paid: 50 000 forward."""
import re

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_money1v_payroll_pg import _closed
from tests.test_p154_money_pg import hold

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
    assert got["total"] == 250_000 and got["lessons_counted"] == 2 and got["state"] == "saved"        # the saved sheet is the truth: money does not change under his eyes


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
