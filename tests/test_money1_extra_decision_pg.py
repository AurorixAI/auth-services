"""Money-1 A3 (ЗП-07, ЗПД-4): the decision on an additional lesson says for how many students and how much BEFORE the confirmation, and can be taken back while the month of the teacher is open.

The world is the one of #154 Ц9: 5 students paid, 3 came (a, b present, c late), d absent, e excused (pending); «Оплатить» pays for all 5 (rule 4.3), 5 x 25 000 = 125 000."""
from datetime import date, datetime

import pytest

from tests.ops_helpers import clock, closed_sheet, grant_money_right, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_p154_money_pg import build_org, decide, extra_world, hold, one_off, sheet, states

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


# ================================================================================================ stage walk (05.11): D-4 / D-5 - the decision in a closed month
from tests.money1_helpers import changed_tables, db_fingerprint, reopen_now  # noqa: E402

OCT = date(2026, 10, 1)
URL = f"{API}/org/one-off-lessons"


def _shut_october(w):
    """The owner saved October of teacher A after it ended (02.11): the month is CLOSED for his lessons."""
    closed_sheet(w, w.A, OCT, saved="2026-11-02 12:00")


def _save_again(w, when):
    """The owner saves the sheet of October once more (``YYYY-MM-DD HH:MM`` Tashkent): its last save moves, a reopening before it is over."""
    from src.auth import business_clock as bc
    from src.models.auth import PayrollSheet

    row = w.db.query(PayrollSheet).filter_by(org_id=w.org.id, teacher_id=w.A.id, period_month=OCT).one()
    row.updated_at = bc.business_wall_to_utc(datetime.strptime(when, "%Y-%m-%d %H:%M"))
    w.db.commit()


def _call(w, who, made, decision, *, dry_run=False, expect=None):
    r = client(w, who).post(f"{URL}/{made}/money-decision", params={"dry_run": "true"} if dry_run else None, json={"decision": decision})
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


def _excuse_refusal(w, g, made, e):
    """The refusal of a decision on an EXCUSE in the same closed month: the pattern the decision on an additional lesson has to repeat (S6)."""
    from src.models.auth import AttendanceExcuse, GroupAttendance

    att = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id=made).one()
    row = w.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=e.id).first()
    if row is None:
        row = AttendanceExcuse(org_id=w.org.id, attendance_id=att.id, student_id=e.id, status="pending", reason="Справка", reason_by=w.owner.id, reason_at=att.created_at)
        w.db.add(row)
        w.db.commit()
    r = client(w, "owner").post(f"{API}/org/attendance/excuses/{row.id}/review", json={"decision": "confirmed"})
    assert r.status_code == 409, r.text
    return r.json()["detail"]


@pytest.mark.parametrize("decision", ["pay", "skip"])
def test_d4_a_decision_in_a_closed_month_is_refused_with_the_refusal_of_an_excuse_and_nothing_is_written(pg_session, clock, decision):
    """Stage walk D-4: «Оплатить» in October (closed for the teacher) was accepted and the saved sheet drifted by +125 000 here (+62 501 on stage).  Now: the SAME 409 as S6 (an excuse in a closed month)."""
    w = build_org(pg_session)
    g, (a, b, c, d, e), ms, made = extra_world(w, clock, "wait", "A")
    _shut_october(w)
    want = _excuse_refusal(w, g, made, e)
    before = db_fingerprint(w.db)
    for who in ("owner", "admin"):
        for dry in (False, True):
            r = _call(w, who, made, decision, dry_run=dry, expect=409)
            assert r.json()["detail"] == want, (who, dry)                                  # the same code, text, month, teacher, day of the closing: «Переоткрыть месяц» is the way out
    assert want["code"] == "PERIOD_CLOSED" and want["month"] == "2026-10" and want["closedOn"] == "2026-11-02" and "переоткройте месяц" in want["message"]
    assert changed_tables(before, db_fingerprint(w.db)) == [] and one_off(w, made).money == "wait"


def test_d4_the_decision_waits_for_the_reopening_and_then_is_accepted_and_the_undo_is_refused_again_after_the_next_closing(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    _shut_october(w)
    _call(w, "owner", made, "pay", expect=409)
    clock.set("2026-11-04 12:00")
    reopen_now(w.db, w.org.id, w.A.id, "2026-10", w.owner.id, "Исправляем октябрь")
    paid = _call(w, "owner", made, "pay", expect=200).json()
    assert paid["money"] == "paid" and paid["month"] == "2026-10" and paid["dryRun"] is False and one_off(w, made).money == "paid"
    _save_again(w, "2026-11-04 15:00")                                                     # the sheet saved again after the correction closes the month again
    clock.set("2026-11-04 16:00")
    before = db_fingerprint(w.db)
    for dry in (False, True):
        assert _call(w, "owner", made, "undo", dry_run=dry, expect=409).json()["detail"]["code"] == "PERIOD_CLOSED"
    assert changed_tables(before, db_fingerprint(w.db)) == [] and one_off(w, made).money == "paid"
    reopen_now(w.db, w.org.id, w.A.id, "2026-10", w.owner.id, "Ещё раз")
    assert _call(w, "owner", made, "undo", expect=200).json()["money"] == "wait"           # after the reopening - again possible


def test_d4_the_preview_of_the_decision_is_the_state_after_it_and_writes_nothing(pg_session, clock):
    """I-5: the window says «ведомость: before -> after» and the sheet then has exactly that.  The owner sees the sums; the administrator only the month and its state (LESSONS 45)."""
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    closed_sheet(w, w.A, OCT, saved="2026-10-30 12:00")                                    # saved in the middle of the month (an advance): the month is OPEN
    before = db_fingerprint(w.db)
    owner_view = _call(w, "owner", made, "pay", dry_run=True, expect=200).json()
    admin_view = _call(w, "admin", made, "pay", dry_run=True, expect=200).json()
    assert changed_tables(before, db_fingerprint(w.db)) == [] and one_off(w, made).money == "wait"
    assert owner_view["dryRun"] is True and owner_view["moneyEffects"]["dryRun"] is True and owner_view["month"] == "2026-10"
    (line,) = owner_view["moneyEffects"]["payroll"]
    assert (line["month"], line["state"], line["freshBefore"], line["freshAfter"], line["freshTotal"], line["changedBy"]) == ("2026-10", "open", 0, 125_000, 125_000, 125_000)
    assert admin_view["moneyEffects"]["payroll"] == [{"month": "2026-10", "state": "open"}] and "125" not in admin_view["moneyEffects"]["text"].replace(" ", "")
    assert "изменится на +125 000" in owner_view["moneyEffects"]["text"]
    done = _call(w, "owner", made, "pay", expect=200).json()
    (after,) = done["moneyEffects"]["payroll"]
    assert after["freshAfter"] == line["freshAfter"] == sheet(w, "2026-10")["total_amount"] and done["moneyEffects"]["dryRun"] is False        # the preview == the state after
    assert "изменилась на +125 000" in done["moneyEffects"]["text"]


def test_d5_the_preview_of_the_undo_says_what_the_sheet_loses_and_writes_nothing(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    closed_sheet(w, w.A, OCT, saved="2026-10-30 12:00")
    _call(w, "owner", made, "pay", expect=200)
    before = db_fingerprint(w.db)
    seen = _call(w, "owner", made, "undo", dry_run=True, expect=200).json()
    assert changed_tables(before, db_fingerprint(w.db)) == [] and one_off(w, made).money == "paid"
    (line,) = seen["moneyEffects"]["payroll"]
    assert (line["freshBefore"], line["freshAfter"], line["changedBy"], line["state"]) == (125_000, 0, -125_000, "open") and seen["month"] == "2026-10"
    real = _call(w, "owner", made, "undo", expect=200).json()
    assert real["moneyEffects"]["payroll"][0]["freshAfter"] == 0 == sheet(w, "2026-10")["total_amount"]
    # the decision «не оплачивать» moves no money: the preview says so (no line), the lesson stays paid for nobody either way
    again = _call(w, "owner", made, "skip", dry_run=True, expect=200).json()
    assert again["moneyEffects"]["payroll"] == [] and one_off(w, made).money == "wait"


def test_d4_the_queue_says_in_advance_which_decisions_are_blocked_by_a_closed_month(pg_session, clock):
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    assert [i["periodClosed"] for i in client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]] == [False]
    _shut_october(w)
    for who in ("owner", "admin"):
        assert [i["periodClosed"] for i in client(w, who).get(f"{API}/org/one-off-decisions").json()["items"]] == [True]     # the button is blocked before the window is opened
    clock.set("2026-11-04 12:00")
    reopen_now(w.db, w.org.id, w.A.id, "2026-10", w.owner.id, "Исправляем октябрь")
    assert [i["periodClosed"] for i in client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]] == [False]


def test_d4_the_preview_keeps_the_right_and_the_refusals_of_the_real_call(pg_session, clock):
    """A preview is not a way round: the same 403 for an administrator without the right, the same 409 for a repeat / an unheld lesson."""
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    grant_money_right(w, False)
    assert _call(w, "admin", made, "pay", dry_run=True).status_code == 403 and _call(w, "A", made, "pay", dry_run=True).status_code == 403
    grant_money_right(w, True)
    _call(w, "admin", made, "pay", expect=200)
    assert _call(w, "admin", made, "skip", dry_run=True, expect=409).json()["detail"]["code"] == "ALREADY_DECIDED"


# ================================================================================================ LESSONS 104: the status of a payout in a warning is THE status of the sheet
def _advance_world(pg_session, clock):
    """October of teacher A: five students paid, additional lesson of 29.10 waits for a decision; a lesson of 21.10 held (125 000); the sheet is saved on 30.10 (open month), an advance of 200 000 is
    paid (the owner confirms the overpay of 75 000); then a lesson of 23.10 is held: the data say 250 000 now."""
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    hold(w, g, "lesson-wed", "2026-10-21", {x: "present" for x in st}, start="10:00", end="11:00")
    clock.set("2026-10-30 12:00")
    calc = sheet(w, "2026-10")
    assert calc["total_amount"] == 125_000
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "expected_fingerprint": calc["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    w.sheet_id = saved.json()["id"]
    url = f"{API}/org/payroll/sheets/{w.sheet_id}/payouts"
    asked = client(w, "owner").post(url, json={"amount": 200_000, "paid_on": "2026-10-30", "method": "cash"})
    assert asked.status_code == 409 and asked.json()["detail"]["overpay"] == 75_000, asked.text
    assert client(w, "owner").post(url, json={"amount": 200_000, "paid_on": "2026-10-30", "method": "cash", "confirm_overpay": True, "overpay": 75_000}).status_code == 201
    hold(w, g, "lesson-fri", "2026-10-23", {x: "present" for x in st}, start="10:00", end="11:00")
    return w, g, made


def _sheet_status(w):
    r = client(w, "owner").get(f"{API}/org/payroll/sheets/{w.sheet_id}")
    assert r.status_code == 200, r.text
    return r.json()["payout"]


def test_the_status_in_the_warnings_is_the_payout_state_of_the_sheet_in_an_open_and_in_a_closed_month(pg_session, clock):
    """Before: ``payrollSheet.status`` compared the payouts with the SAVED total only: in the open month the advance of 200 000 against the saved 125 000 said «переплачено» while the sheet said «Месяц открыт:
    начислено сейчас 250 000, выплачено 200 000, к выплате 50 000» (``due``)."""
    from src.auth import payroll_hooks

    w, g, made = _advance_world(pg_session, clock)
    on_sheet = _sheet_status(w)
    assert (on_sheet["basis"], on_sheet["state"], on_sheet["balance"]) == ("fresh", "due", 50_000)
    (item,) = client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]
    assert item["payrollSheet"] == {"month": "2026-10", "status": on_sheet["state"]}      # the queue (the owner)
    hook = payroll_hooks.sheets_touched_by_lesson_teacher_change(w.db, w.org.id, "2026-10-21", w.A.id, w.B.id, owner=True)
    assert [h["status"] for h in hook] == ["due"]                                                           # the warning of a change of the teacher
    assert payroll_hooks.sheets_touched_by_lesson_teacher_change(w.db, w.org.id, "2026-10-21", w.A.id, w.B.id, owner=False) == [{"month": "2026-10"}]      # the administrator: the month only
    # the same month, CLOSED (the sheet is saved again after it ended): by the saved total, the same words as the sheet
    clock.set("2026-11-02 12:00")
    calc = sheet(w, "2026-10")
    again = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "revision": 1, "expected_fingerprint": calc["source_fingerprint"]})
    assert again.status_code in (200, 201), again.text
    on_sheet = _sheet_status(w)
    assert (on_sheet["basis"], on_sheet["saved_total"], on_sheet["paid"], on_sheet["state"]) == ("saved", 250_000, 200_000, "due")
    (item,) = client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]
    assert item["payrollSheet"]["status"] == on_sheet["state"]
    assert [h["status"] for h in payroll_hooks.sheets_touched_by_lesson_teacher_change(w.db, w.org.id, "2026-10-21", w.A.id, w.B.id, owner=True)] == [on_sheet["state"]]
    # overpaid in the closed month: the same word on both
    pay_more = client(w, "owner").post(f"{API}/org/payroll/sheets/{w.sheet_id}/payouts", json={"amount": 100_000, "paid_on": "2026-11-02", "method": "cash", "confirm_overpay": True, "overpay": 50_000})
    assert pay_more.status_code == 201, pay_more.text
    on_sheet = _sheet_status(w)
    assert on_sheet["state"] == "overpaid"
    assert [h["status"] for h in payroll_hooks.sheets_touched_by_lesson_teacher_change(w.db, w.org.id, "2026-10-21", w.A.id, w.B.id, owner=True)] == ["overpaid"]


def test_the_effects_say_what_is_left_to_pay_after_the_decision_by_the_payout_function_of_the_list(pg_session, clock):
    """``moneyEffects.payroll[].balanceAfter`` («Уже выплачено 200 000 — до новой суммы останется …»): the October of the advance world (the data say 250 000, paid 200 000; the decision «Включить в
    зарплату» adds 5 x 25 000) - the preview says the number that the sheet, the list of the month and the real answer say after it (LESSONS 104: one number, one function)."""
    w, g, made = _advance_world(pg_session, clock)
    seen = _call(w, "owner", made, "pay", dry_run=True, expect=200).json()
    (line,) = seen["moneyEffects"]["payroll"]
    assert (line["freshBefore"], line["freshAfter"], line["paid"], line["balanceAfter"]) == (250_000, 375_000, 200_000, 175_000)
    admin_line = _call(w, "admin", made, "pay", dry_run=True, expect=200).json()["moneyEffects"]["payroll"]
    assert admin_line == [{"month": "2026-10", "state": "open"}]                         # the administrator: no sums, no balance
    done = _call(w, "owner", made, "pay", expect=200).json()
    (after,) = done["moneyEffects"]["payroll"]
    assert after["balanceAfter"] == line["balanceAfter"] == _sheet_status(w)["balance"] == 175_000
    month = client(w, "owner").get(f"{API}/org/payroll/months/2026-10").json()["teachers"]
    assert [t["payout"]["balance"] for t in month if t["payout"]] == [175_000]            # the list of the month says the same
    # paid ahead: a decision that takes the lesson back leaves less than was paid - the balance is negative (paid ahead), never hidden
    undo = _call(w, "owner", made, "undo", dry_run=True, expect=200).json()["moneyEffects"]["payroll"][0]
    assert (undo["freshAfter"], undo["balanceAfter"]) == (250_000, 50_000)


def test_the_status_in_the_change_of_the_lesson_teacher_is_the_payout_state_of_the_sheet(pg_session, clock):
    """The route itself (an open month): the answer of ``PUT …/lesson-teacher`` carries ``payrollSheetsAffected[].status`` = the state of the payout of the sheet."""
    w, g, made = _advance_world(pg_session, clock)
    want = _sheet_status(w)["state"]
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/lesson-teacher", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "teacherId": str(w.B.id), "reason": "Провёл другой преподаватель"})
    assert r.status_code == 200, r.text
    assert [(x["month"], x["status"]) for x in r.json()["payrollSheetsAffected"] if x.get("teacher_id") == str(w.A.id)] == [("2026-10", want)] and want == "due"
