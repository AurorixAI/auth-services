"""Money-1 A3 (ЗП-07, ЗПД-4): the decision on an additional lesson says for how many students and how much BEFORE the confirmation, and can be taken back while the month of the teacher is open.

The world is the one of #154 Ц9: 5 students paid, 3 came (a, b present, c late), d absent, e excused (pending); «Оплатить» pays for all 5 (rule 4.3), 5 x 25 000 = 125 000."""
from datetime import date

from tests.ops_helpers import clock, closed_sheet, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_p154_money_pg import build_org, decide, extra_world, one_off, sheet, states

pytestmark = needs_pg


def test_the_queue_says_how_many_students_the_decision_pays_for_and_the_owner_sees_the_sum(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    (owner_item,) = client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]
    assert (owner_item["studentsPresent"], owner_item["studentsTotal"], owner_item["studentsPaid"]) == (3, 5, 5)          # «пришло 3 из 8, платится за 8»
    assert owner_item["moneyEstimate"] is not None and owner_item["moneyEstimate"]["amount"] == 125_000
    (admin_item,) = client(w, "admin").get(f"{API}/org/one-off-decisions").json()["items"]
    assert admin_item["studentsPaid"] == 5 and admin_item["moneyEstimate"] is None                                         # LESSONS 45: the pay of a teacher is the owner's


def test_the_decision_can_be_taken_back_in_an_open_month_and_not_in_a_closed_one(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    decide(w, "admin", made, "pay")
    assert sheet(w, "2026-10")["total_amount"] == 125_000 and states(sheet(w, "2026-10"), made, st) == ["counted"] * 5
    undone = decide(w, "admin", made, "undo").json()
    assert undone["money"] == "wait"
    assert states(sheet(w, "2026-10"), made, st) == ["undecided"] * 5 and sheet(w, "2026-10")["total_amount"] == 0
    assert decide(w, "admin", made, "undo").json()["money"] == "wait"                                                      # a repeat changes nothing
    decide(w, "owner", made, "skip")                                                                                       # and it can be decided again
    assert one_off(w, made).money == "none"
    closed_sheet(w, w.A, date(2026, 10, 1), saved="2026-11-02 12:00")                                                      # the owner saved October after it ended
    shut = decide(w, "owner", made, "undo", expect=409)
    assert shut.json()["detail"]["code"] == "PERIOD_CLOSED"
    assert one_off(w, made).money == "none"
