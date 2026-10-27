"""PAY-F, the fix round after R3 (PostgreSQL): the kind of a cancellation by role, the lessons that are in the price of the month, the horizon of the next period, the
robust day off (exam-service down, idempotent, void trace, overlaps), the forecast of a lesson that is over, the income report by the month of the lesson."""
from datetime import datetime

import pytest
from sqlalchemy import text

from src.auth import ledger_service as ls
from src.auth import payment_status as ps
from src.auth.payments_income import IncomeReports
from src.auth.service import AuthService
from src.models.auth import AttendanceExcuse, GroupAttendance, GroupAttendanceAudit, GroupAudit, OrgDayOff

from tests.ops_helpers import clock, history_row, needs_pg, new_group, new_slot, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, build_org, cancel, centre, client, give_group_price, pay, rows, set_payments, slots, statement
from tests.test_payf_recalc_pg import month_world, row_of

pytestmark = needs_pg
LINE_SQL = "SELECT cancellation_kind FROM group_attendance WHERE group_id=:g AND lesson_date=:d AND lesson_id=:l"


def kind_of(w, g, day, lesson):
    w.db.expire_all()
    return w.db.execute(text(LINE_SQL), {"g": g.id, "d": day, "l": lesson}).scalar_one()


def recalc_days(w, m):
    return [r["date"] for r in statement(w, m)["rows"] if r["type"] == "recalc"]


# ---- 1. the kind of a cancellation by role ----------------------------------------------------------------------------------------------------------------


def test_1_a_teacher_cannot_choose_a_holiday_and_gets_a_clear_403(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    r = cancel(w, g, "lesson-mon", "2026-10-26", kind="holiday", who="A", expect=403)
    assert r.json()["detail"]["code"] == "TEACHER_CANNOT_CANCEL"                                  # W1 S3: a teacher does not cancel a lesson himself at all, whatever kind he sends
    assert w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-10-26").count() == 0          # nothing was written, nothing replaced silently
    missed = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/reconcile/cancel", json={
        "lessonDate": "2026-10-19", "lessonId": "lesson-mon", "cancellationReason": "Преподаватель заболел", "cancellationKind": "holiday"})
    assert missed.status_code == 403 and "только владелец или администратор" in missed.json()["detail"]


def test_1_a_cancellation_without_a_kind_is_by_the_fault_of_the_centre_and_the_audit_keeps_the_kind(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    cancel(w, g, "lesson-mon", "2026-10-26")
    assert kind_of(w, g, "2026-10-26", "lesson-mon") == "centre" and row_of(w, s)["debt"] == 554_000
    cancel(w, g, "lesson-wed", "2026-10-28", kind="centre")                                      # saying «centre» is allowed
    audit = w.db.query(GroupAttendanceAudit).filter_by(group_id=g.id, action="cancelled").all()
    assert sorted(a.details["cancellation_kind"] for a in audit) == ["centre", "centre"]
    client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/reconcile/cancel", json={"lessonDate": "2026-10-19", "lessonId": "lesson-mon", "cancellationReason": "Заболел"})
    missed = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-10-19").one()
    assert missed.cancellation_kind == "centre"
    assert w.db.query(GroupAttendanceAudit).filter_by(attendance_id=missed.id).one().details == {"cancellation_kind": "centre"}


def test_1_an_owner_or_admin_chooses_and_changes_the_kind_of_a_cancelled_lesson(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    cancel(w, g, "lesson-wed", "2026-10-21")                                                   # «по вине центра» by default
    assert recalc_days(w, m) == ["2026-10-21"] and row_of(w, s)["debt"] == 554_000
    put = lambda who, body, expect=200: (lambda r: (r.status_code == expect or pytest.fail(r.text), r)[1])(  # noqa: E731
        client(w, who).put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", **body}))
    put("A", {"cancellationKind": "holiday", "reason": "Праздник"}, 403)                       # a teacher may not
    put("admin", {"cancellationKind": "holiday"}, 422)                                         # a reason is required
    r = put("admin", {"cancellationKind": "holiday", "reason": "Это был праздник"}).json()
    assert r["changed"] is True and r["cancellationKind"] == "holiday"
    assert recalc_days(w, m) == [] and row_of(w, s)["debt"] == 600_000                          # the line disappears by itself
    put("owner", {"cancellationKind": "centre", "reason": "Ошиблись: это вина центра"})
    assert recalc_days(w, m) == ["2026-10-21"] and row_of(w, s)["debt"] == 554_000              # and comes back
    assert put("owner", {"cancellationKind": "centre", "reason": "ещё раз"}).json()["changed"] is False
    audit = w.db.query(GroupAttendanceAudit).filter_by(group_id=g.id, action="corrected").order_by(GroupAttendanceAudit.created_at).all()
    assert [(a.details["cancellation_kind"]["from"], a.details["cancellation_kind"]["to"], a.correction_reason) for a in audit] == [
        ("centre", "holiday", "Это был праздник"), ("holiday", "centre", "Ошиблись: это вина центра")]


def test_1_the_kind_is_not_changed_for_a_lesson_that_is_not_cancelled_a_day_off_or_a_move(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    body = lambda day, lesson: {"lessonDate": day, "lessonId": lesson, "cancellationKind": "centre", "reason": "Причина"}      # noqa: E731
    put = lambda day, lesson: client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json=body(day, lesson))      # noqa: E731
    assert put("2026-10-21", "lesson-wed").status_code == 404                                    # not cancelled
    client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-26", "dateTo": "2026-10-26", "reason": "Хайит"})
    r = put("2026-10-26", "lesson-mon")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CANCELLED_BY_DAY_OFF"
    client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={
        "lessonId": "lesson-wed", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "10:00", "endTime": "11:00", "reason": "Перенос по просьбе центра"})
    r = put("2026-10-28", "lesson-wed")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CANCELLATION_HAS_NO_KIND"


# ---- 3. only the lessons that are in the price of the month ----------------------------------------------------------------------------------------------


def one_off(w, g, day, start="15:00", end="16:00"):
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": day, "startTime": start, "endTime": end})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_3_an_extra_one_off_lesson_costs_nothing_and_two_cancellations_of_a_day_are_one_line(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    extra = one_off(w, g, "2026-10-28")
    cancel(w, g, extra, "2026-10-28")                                                         # the extra lesson only
    assert kind_of(w, g, "2026-10-28", extra) == "centre"
    assert recalc_days(w, m) == [] and row_of(w, s)["debt"] == 600_000
    cancel(w, g, "lesson-wed", "2026-10-28")                                                  # the regular lesson of the same day
    # exactly one line, priced by the REGULAR schedule (600 000 / 13): the extra lesson of the period does not cheapen the deducted lesson
    assert recalc_days(w, m) == ["2026-10-28"] and row_of(w, s)["debt"] == 600_000 - 46_000 == 554_000


def test_3_the_copy_of_a_moved_lesson_is_in_the_price_when_the_centre_cancels_it(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={
        "lessonId": "lesson-wed", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "10:00", "endTime": "11:00", "reason": "Перенос по просьбе центра"})
    copy = r.json()["result"]["newLesson"]["id"]
    assert row_of(w, s)["debt"] == 600_000                                                    # the move itself is neutral
    cancel(w, g, copy, "2026-10-29")
    assert recalc_days(w, m) == ["2026-10-29"] and row_of(w, s)["debt"] == 554_000


# ---- 4. the horizon of the next period ------------------------------------------------------------------------------------------------------------------


def test_4_paid_in_full_the_next_period_asks_for_the_price_minus_the_deduction_before_it_begins(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock, paid=600_000)
    cancel(w, g, "lesson-wed", "2026-10-21")                                                  # today is 25.10, the next period begins 06.11
    due = client(w, "owner").get(f"{API}/org/memberships/{m.id}/payments/preview").json()
    assert due["suggested_amount"] == 554_000
    assert row_of(w, s)["advance"] == 46_000 and row_of(w, s)["debt"] == 0
    after = client(w, "owner").get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 554_000}).json()["result"]
    assert after["debt_after"] == 0 and after["paid_until_after"] == "2026-12-05"           # 554 000 + the deduction pay the whole next period (06.11-05.12)


# ---- 6. the robust day off ----------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def three_groups(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-10-19 12:00")
    gs, ss, ms = centre(w, groups=3)
    return w, gs, ss, ms


def exam_stub(monkeypatch, behaviour):
    calls = []

    def check(self, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
        calls.append((phase, group_id, lesson_date))
        behaviour(phase, group_id, lesson_date)

    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", check)
    return calls


def down(phase, group_id, date):
    raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён")


DAY = {"dateFrom": "2026-10-21", "dateTo": "2026-10-21", "reason": "Хайит"}
DOWN_TEXT = "сервис экзаменов недоступен, повторите"


def test_6_the_preview_stops_asking_when_exam_service_is_down(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    calls = exam_stub(monkeypatch, down)
    plan = client(w, "owner").post(f"{API}/org/days-off/preview", json=DAY).json()
    assert len(calls) == 1                                                                    # one question, not N waits
    assert plan["examServiceDown"] is True and plan["cancellable"] == [] and [b["reason"] for b in plan["blocked"]] == [DOWN_TEXT] * 3
    assert "Экзамены, снятые с уроков" in plan["examsNote"]


def test_6_create_stops_on_the_first_timeout_and_apply_finishes_it_idempotently(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    calls = exam_stub(monkeypatch, down)
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY).json()
    assert len(calls) == 1 and out["complete"] is False and out["cancelled"] == [] and [b["reason"] for b in out["blocked"]] == [DOWN_TEXT] * 3
    day_off = out["dayOff"]["id"]
    # the exam-service is back: the same range and reason continue THAT day off (no duplicate)
    exam_stub(monkeypatch, lambda *a: None)
    again = c.post(f"{API}/org/days-off", json=DAY).json()
    assert again["dayOff"]["id"] == day_off and again["complete"] is True and len(again["cancelled"]) == 3 and again["blocked"] == []
    assert w.db.query(OrgDayOff).filter_by(org_id=w.org.id).count() == 1
    done = c.post(f"{API}/org/days-off/{day_off}/apply").json()                               # idempotent
    assert done["cancelled"] == [] and done["alreadyCancelled"] == 3 and done["complete"] is True
    assert client(w, "A").post(f"{API}/org/days-off/{day_off}/apply").status_code == 403
    assert c.post(f"{API}/org/days-off/{day_off}/void").status_code == 200
    assert c.post(f"{API}/org/days-off/{day_off}/apply").status_code == 409                   # a voided day off is not continued
    # a different reason is a different day off
    assert c.post(f"{API}/org/days-off", json={**DAY, "reason": "Навруз"}).json()["dayOff"]["id"] != day_off


def test_6_apply_finishes_what_the_exam_service_stopped(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    state = {"n": 0}

    def flaky(phase, group_id, date):
        state["n"] += 1
        if state["n"] > 2:                                                                     # the first lesson goes (check + release), then the service dies
            raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён")

    exam_stub(monkeypatch, flaky)
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY).json()
    assert len(out["cancelled"]) == 1 and out["complete"] is False and [b["reason"] for b in out["blocked"]] == [DOWN_TEXT] * 2
    exam_stub(monkeypatch, lambda *a: None)
    done = c.post(f"{API}/org/days-off/{out['dayOff']['id']}/apply").json()
    assert len(done["cancelled"]) == 2 and done["alreadyCancelled"] == 1 and done["complete"] is True


def test_6_an_unexpected_error_in_one_lesson_does_not_stop_the_others(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups

    def boom(phase, group_id, date):
        if group_id == str(gs[1].id) and phase == "cancellation-check":
            raise KeyError("something unexpected")

    exam_stub(monkeypatch, boom)
    out = client(w, "owner").post(f"{API}/org/days-off", json=DAY).json()
    assert len(out["cancelled"]) == 2 and [b["groupName"] for b in out["blocked"]] == ["Группа 2"] and out["blocked"][0]["reason"] == "Не удалось отменить урок"


# ---- 7. the trace of a voided day off, 8. overlaps and manual cancellations -----------------------------------------------------------------------------


def test_7_voiding_keeps_the_trace_of_what_it_gave_back(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    exam_stub(monkeypatch, lambda *a: None)
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY).json()
    voided = c.post(f"{API}/org/days-off/{out['dayOff']['id']}/void").json()
    assert "Экзамены, снятые с уроков" in voided["examsNote"] and len(voided["restored"]) == 3
    assert w.db.query(GroupAttendance).filter_by(org_id=w.org.id, lesson_date="2026-10-21").count() == 0
    w.db.expire_all()
    row = w.db.query(OrgDayOff).filter_by(id=out["dayOff"]["id"]).one()
    assert sorted(r["groupName"] for r in row.restored) == ["Группа 1", "Группа 2", "Группа 3"]
    assert {(r["lessonId"], r["date"], r["reason"]) for r in row.restored} == {("lesson-wed", "2026-10-21", "Выходной (Хайит)")}
    assert all(r["cancelledBy"] and r["cancelledAt"] for r in row.restored)
    audit = w.db.query(GroupAudit).filter_by(org_id=w.org.id, action="day_off_voided").all()
    assert sorted(a.snapshot["restored"][0]["groupName"] for a in audit) == ["Группа 1", "Группа 2", "Группа 3"]
    assert all(a.snapshot["day_off_id"] == out["dayOff"]["id"] and a.actor_role == "owner" for a in audit)


def test_8_a_lesson_under_another_live_day_off_is_not_given_back(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    exam_stub(monkeypatch, lambda *a: None)
    c = client(w, "owner")
    first = c.post(f"{API}/org/days-off", json=DAY).json()
    second = c.post(f"{API}/org/days-off", json={"dateFrom": "2026-10-21", "dateTo": "2026-10-23", "reason": "Навруз"}).json()
    assert second["alreadyCancelled"] == 3 and len(second["cancelled"]) == 3                      # the 21st is already cancelled; the 19th was in the past of the range? no: 21..23
    voided = c.post(f"{API}/org/days-off/{first['dayOff']['id']}/void").json()
    assert voided["restored"] == [] and {k["reason"] for k in voided["kept"]} == {"Дата входит в другой выходной"}
    assert w.db.query(GroupAttendance).filter_by(org_id=w.org.id, lesson_date="2026-10-21").count() == 3
    # once the other one is voided too, nothing holds the 21st
    # W1 S10 (Д4): the lessons of the 21st that the first day off cancelled were handed to the second one when the first was voided - nobody is left as a zombie
    gone = c.post(f"{API}/org/days-off/{second['dayOff']['id']}/void").json()
    assert {r["date"] for r in gone["restored"]} == {"2026-10-21", "2026-10-23"}
    assert w.db.query(GroupAttendance).filter_by(org_id=w.org.id, status="cancelled").count() == 0


def test_8_a_manual_cancellation_of_the_centre_becomes_a_holiday(three_groups, monkeypatch):
    w, gs, ss, ms = three_groups
    set_payments(w, centre_cancel_recalc=False, recalc_history=[{"from": "2026-10-01", "on": True}])
    exam_stub(monkeypatch, lambda *a: None)
    cancel(w, gs[0], "lesson-wed", "2026-10-21")                                              # by hand, «по вине центра»
    assert kind_of(w, gs[0], "2026-10-21", "lesson-wed") == "centre"
    c = client(w, "owner")
    plan = c.post(f"{API}/org/days-off/preview", json={k: v for k, v in DAY.items() if k != "reason"}).json()
    assert [l["groupName"] for l in plan["convertible"]] == ["Группа 1"] and len(plan["cancellable"]) == 2
    out = c.post(f"{API}/org/days-off", json=DAY).json()
    assert [l["groupName"] for l in out["converted"]] == ["Группа 1"] and len(out["cancelled"]) == 2
    assert kind_of(w, gs[0], "2026-10-21", "lesson-wed") == "holiday"
    audit = w.db.query(GroupAttendanceAudit).filter_by(group_id=gs[0].id, action="corrected").one()
    assert audit.details["cancellation_kind"] == {"from": "centre", "to": "holiday"} and audit.details["day_off_id"] == out["dayOff"]["id"]
    # a voided day off does not take that lesson back: the author cancelled it, it stays cancelled - and its kind (the centre's fault) comes back (W1 R3 c)
    voided = c.post(f"{API}/org/days-off/{out['dayOff']['id']}/void").json()
    assert len(voided["restored"]) == 2
    assert [k["reason"] for k in voided["kept"]] == ["Ручная отмена: вид «по вине центра» возвращён"]
    assert kind_of(w, gs[0], "2026-10-21", "lesson-wed") == "centre"


# ---- 12. the forecast: a lesson that is over today, the end of the window --------------------------------------------------------------------------


def test_12_a_lesson_of_today_that_is_over_and_the_student_was_excused_is_not_in_the_future(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-10-21 18:00")
    (g,), (s,), (m,) = centre(w, joined=datetime(2026, 9, 1, 6, 0), period_mode="lessons", lessons_per_payment=12)
    pay(w, m, 600_000, D(2026, 10, 21))
    held = history_row(w.db, w.org, g, "lesson-wed", "2026-10-21", w.A, [s], start="10:00", end="10:55", records={str(s.id): "excused"})
    w.db.add(AttendanceExcuse(org_id=w.org.id, attendance_id=held.id, student_id=s.id, reason="Болел", reason_by=w.A.id, reason_at=datetime(2026, 10, 21, 9),
                              status="confirmed", reviewed_by=w.owner.id, reviewed_at=datetime(2026, 10, 21, 12)))
    w.db.commit()
    (row,) = rows(w, s)
    # 23, 26, 28, 30, 02.11, 04.11, 06.11, 09.11, 11.11, 13.11, 16.11, 18.11: the lesson of today (over, he was excused) is not one of the twelve
    assert row["lessons_left"] == 12 and row["paid_through_forecast"] == "2026-11-18"


def test_12_no_forecast_when_the_window_of_400_days_ends_before_the_package(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-10-05 12:00")
    set_payments(w, period_mode="lessons", lessons_per_payment=60)
    from tests.ops_helpers import new_student

    g = new_group(w.db, w.org, w.ids, "GW", "Раз в неделю", w.A, [new_slot("lesson-mon", 1, "10:00", "11:00", "2026-01-01")])
    s = new_student(w.db, w.org, w.ids, "sw", "Недельный", "Ученик", g, joined=datetime(2026, 9, 1, 6, 0))
    give_group_price(w.db, g, 1_200_000, valid_from=D(2026, 1, 1))
    w.db.commit()
    from src.models.auth import StudentGroup

    m = w.db.query(StudentGroup).filter_by(group_id=g.id, student_id=s.id).one()
    pay(w, m, 1_200_000, D(2026, 10, 5))
    (row,) = rows(w, s)
    assert row["lessons_left"] == 60 and row["paid_through_forecast"] is None                 # 60 weekly lessons are 420 days: the schedule does not reach


# ---- 16. the income report puts the deduction in the month of the LESSON --------------------------------------------------------------------------------


def test_16_the_income_report_reduces_the_month_of_the_cancelled_lesson(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock, paid=600_000)
    cancel(w, g, "lesson-mon", "2026-11-02")                                                  # a lesson of the period 06.10-05.11, but of NOVEMBER
    clock.set("2026-11-10 12:00")
    loader = ls.LedgerLoader(w.db, str(w.org.id), ps.business_today())
    shown = loader.load(scope="all")
    october = IncomeReports._collection(shown, D(2026, 10, 1), D(2026, 10, 31))
    november = IncomeReports._collection(shown, D(2026, 11, 1), D(2026, 11, 30))
    assert (october.total.charged, october.total.paid, october.total.debt) == (600_000, 600_000, 0)       # October's target is not touched
    assert (november.total.charged, november.total.paid, november.total.debt) == (554_000, 0, 554_000)    # November's target shrank by the lesson of November


def test_1_the_journal_says_the_kind_of_every_cancelled_lesson(pg_session, clock, monkeypatch):
    """The sheet of a cancelled lesson offers «Изменить причину отмены» by this map: `centre` / `holiday` / `day_off`; a move and an old cancellation are absent."""
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    exam_stub(monkeypatch, lambda *a: None)
    cancel(w, g, "lesson-wed", "2026-10-21")                                                    # centre
    cancel(w, g, "lesson-fri", "2026-10-23", kind="holiday", reason="Хайит")                     # holiday
    client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-26", "dateTo": "2026-10-26", "reason": "Навруз"})
    client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={
        "lessonId": "lesson-wed", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "10:00", "endTime": "11:00", "reason": "Перенос по просьбе центра"})
    kinds = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-10"}).json()["cancellationKinds"]
    assert kinds == {"2026-10-21_lesson-wed": "centre", "2026-10-23_lesson-fri": "holiday", "2026-10-26_lesson-mon": "day_off"}      # the move has none
