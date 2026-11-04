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


def _extra_estimate_and_sheet(w, who="owner"):
    (item,) = client(w, who).get(f"{API}/org/one-off-decisions").json()["items"]
    return item


def test_r3_19_scenario_a_the_sum_before_the_decision_is_the_sum_after_it_with_a_confirmed_excuse(pg_session, clock):
    """R3-деньги НЕТОЧНОСТЬ-4, A: five in the roster, three came, one has a CONFIRMED excuse -> «за 4 / 100 000»; before the round the screen said 5 / 125 000 and the sheet got 100 000."""
    from src.models.auth import AttendanceExcuse, GroupAttendance

    w = build_org(pg_session)
    g, (a, b, c, d, e), ms, made = extra_world(w, clock, "wait", "A")
    att = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id=made).one()
    row = w.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=e.id).first()
    if row is None:
        row = AttendanceExcuse(org_id=w.org.id, attendance_id=att.id, student_id=e.id, status="confirmed", reason="Справка", reason_by=w.owner.id, reason_at=att.created_at,
                               reviewed_by=w.owner.id, reviewed_at=att.created_at)
        w.db.add(row)
    else:
        row.status = "confirmed"
    w.db.commit()
    item = _extra_estimate_and_sheet(w)
    assert item["studentsPaid"] == 4 and item["moneyEstimate"] == {"amount": 100_000, "reason": None}
    decide(w, "owner", made, "pay")
    assert sheet(w, "2026-10")["total_amount"] == 100_000                                                                 # before = after


def test_r3_19_scenario_b_a_student_who_did_not_pay_is_still_in_the_sum_of_an_additional_lesson(pg_session, clock):
    """B: one of the five paid nothing; an additional lesson is paid independently of the payment of the student -> 125 000 before and after (before the round: 100 000 / 125 000)."""
    from src.models.auth import StudentPayment

    w = build_org(pg_session)
    g, (a, b, c, d, e), ms, made = extra_world(w, clock, "wait", "A")
    w.db.query(StudentPayment).filter(StudentPayment.student_id == d.id).delete(synchronize_session=False)
    w.db.commit()
    item = _extra_estimate_and_sheet(w)
    decide(w, "owner", made, "pay")
    after = sheet(w, "2026-10")["total_amount"]
    assert item["moneyEstimate"]["amount"] == after == 125_000
