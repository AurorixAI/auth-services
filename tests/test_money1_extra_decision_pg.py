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


def test_r3_28_the_list_of_decided_additional_lessons_is_for_the_money_right_and_says_when_the_decision_can_be_taken_back(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    url = f"{API}/org/one-off-decisions/decided"
    assert client(w, "owner").get(url).json() == {"total": 0, "items": []}                                                  # nothing decided yet
    decide(w, "admin", made, "pay")
    assert client(w, "owner").get(f"{API}/org/one-off-decisions").json()["total"] == 0                                      # it left the queue ...
    (item,) = client(w, "owner").get(url).json()["items"]                                                                    # ... and is in the list of the decided
    assert item["lessonId"] == made and item["money"] == "paid" and item["groupName"] and (item["studentsPresent"], item["studentsPaid"]) == (3, 5)
    assert item["canUndo"] is True and item["periodClosed"] is False and item["decidedBy"]["role"] == "admin" and item["decidedAt"]
    assert client(w, "owner").get(url, params={"month": "2026-10"}).json()["total"] == 1 and client(w, "owner").get(url, params={"month": "2026-09"}).json()["total"] == 0
    closed_sheet(w, w.A, date(2026, 10, 1), saved="2026-11-02 12:00")
    (shut,) = client(w, "owner").get(url).json()["items"]
    assert shut["canUndo"] is False and shut["periodClosed"] is True                                                         # the button «Отменить решение» is not offered: reopen the month first
    assert client(w, "A").get(url).status_code == 403                                                                        # a teacher
    assert client(w, "admin").get(url).status_code == 200                                                                    # this world's administrator has the money right (``decide`` above)
    assert client(w, "owner").post(f"{API}/org/one-off-lessons/{made}/money-decision", json={"decision": "undo"}).status_code == 409
