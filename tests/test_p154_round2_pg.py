"""#154, the round of fixes after R3 (PostgreSQL): S1 copies of a move, S2 a make-up whose day has passed, S3 a day off under a removed make-up, S4/S5 the words of the sheet, S7 no default role,
S8 the reason of a cancellation for a teacher, S9 overlaps of an additional lesson, S10 the order of the locks / the return of a lesson / the migration.  The chains of the spec are in
``test_p154_create_pg.py`` and ``test_p154_money_pg.py``; the helpers are theirs."""
from datetime import date, datetime
from uuid import uuid4

import pytest

from src.auth.ops_rules import OpsError
from src.models.auth import GroupAttendance, GroupScheduleOneOffLesson, LessonSubstitutionRequest
from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session, row_of, run_parallel  # noqa: F401  (fixtures)
from tests.ops_helpers import client, code_of
from tests.payf_helpers import build_org, cancel
from tests.test_p154_create_pg import ORIGIN, ORIGIN_DAY, cancel_origin, makeup, move, one_off, pointer, w  # noqa: F401  (w: fixture)
from tests.test_p154_create_pg import make as make_d2
from tests.test_p154_money_pg import API as _API  # noqa: F401
from tests.test_p154_money_pg import decide, deducted, extra_world, five, hold, sheet as pay_sheet
from tests.test_p154_money_pg import make as make_money
from tests.test_p154_money_pg import makeup as makeup_money
from tests.test_w1_cancel_pg import code, restore, sheet

pytestmark = needs_pg


def move_money(w, g, lesson, day, new_day):
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": lesson, "lessonDate": day, "newDate": new_day, "startTime": "12:00", "endTime": "13:00", "reason": "Перенос"})
    assert r.status_code == 200, r.text
    return r.json()["result"]["newLesson"]["id"]


# ============================================================================================================ S1: the copies of a move (LESSONS 53)
def test_s1_the_cancelled_copy_of_a_moved_additional_lesson_deducts_nothing(pg_session, clock):
    w = build_org(pg_session)
    g, students, ms = five(w, clock)                                                   # the lesson of 26.10 is cancelled by the centre: 46 000 each
    extra = make_money(w, "admin", g, "2026-10-29", purpose="extra", money="none").json()["id"]
    copy = move_money(w, g, extra, "2026-10-29", "2026-10-30")
    assert one_off(w, copy).purpose == "extra"
    cancel(w, g, copy, "2026-10-30")                                                   # the copy is cancelled «по вине центра» (the default kind)
    assert [deducted(w, m) for m in ms] == [46_000] * 5                                # an additional lesson was never in the price of the month


def test_s1_the_cancelled_copy_of_a_moved_make_up_does_not_double_the_deduction_of_the_original(pg_session, clock):
    w = build_org(pg_session)
    g, students, ms = five(w, clock)
    mk = makeup_money(w, "admin", g, "2026-10-29", "lesson-mon", "2026-10-26").json()["id"]
    copy = move_money(w, g, mk, "2026-10-29", "2026-10-30")
    assert [deducted(w, m) for m in ms] == [0] * 5                                     # the make-up is ahead (the pointer is on the copy)
    cancel(w, g, copy, "2026-10-30")
    assert pointer_of(w, g) is None                                                    # the original is free again ...
    assert [deducted(w, m) for m in ms] == [46_000] * 5                                # ... and costs once, not twice


def test_s1_the_cancelled_copy_of_a_moved_regular_lesson_still_deducts(pg_session, clock):
    w = build_org(pg_session)
    g, students, ms = five(w, clock)
    copy = move_money(w, g, "lesson-wed", "2026-10-28", "2026-10-29")
    assert one_off(w, copy).purpose == "moved"
    cancel(w, g, copy, "2026-10-29")
    assert [deducted(w, m) for m in ms] == [92_000] * 5                                # the Monday lesson and the copy (a lesson of the price of the month)


def pointer_of(w, g, lesson="lesson-mon", day="2026-10-26"):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id=lesson, lesson_date=day).one().made_up_by


# ============================================================================================================ S2: a make-up whose day has passed (LESSONS 54)
def test_s2_a_make_up_that_was_never_held_stops_pausing_the_deduction_and_the_journal_entered_later_lifts_it(pg_session, clock):
    w = build_org(pg_session)
    g, (a, b, c, d, e), ms = five(w, clock)
    mk = makeup_money(w, "admin", g, "2026-10-29", "lesson-mon", "2026-10-26").json()["id"]
    clock.set("2026-10-29 09:00")
    assert [deducted(w, m) for m in ms] == [0] * 5                                     # the day of the make-up: still ahead
    clock.set("2026-10-30 09:00")
    assert [deducted(w, m) for m in ms] == [46_000] * 5                                # yesterday's make-up without a journal row = «не проведена»: the deduction acts
    hold(w, g, mk, "2026-10-29", {a: "present", b: "late", c: "present", d: "absent", e: "excused"}, confirmed=[e])
    assert [deducted(w, m) for m in ms] == [0, 0, 0, 0, 46_000]                         # the facts entered later lift it as always


def test_s2_a_make_up_that_is_going_on_stays_ahead_only_today(pg_session, clock):
    from tests.ops_helpers import history_row

    w = build_org(pg_session)
    g, (a, b, c, d, e), ms = five(w, clock)
    mk = makeup_money(w, "admin", g, "2026-10-29", "lesson-mon", "2026-10-26").json()["id"]
    clock.set("2026-10-29 12:30")
    row = history_row(w.db, w.org, g, mk, "2026-10-29", w.A, [a, b, c, d, e], status="in_progress", start="12:00", end="13:00")
    w.db.commit()
    assert [deducted(w, m) for m in ms] == [0] * 5                                     # started today and not closed: the lesson is going on
    clock.set("2026-10-30 09:00")
    assert [deducted(w, m) for m in ms] == [46_000] * 5                                # yesterday's lesson left open = «не проведена»: the deduction acts (LESSONS 54)
    row = w.db.query(GroupAttendance).filter_by(id=row.id).one()
    row.status, row.records = "completed", {str(a.id): "present", str(b.id): "present", str(c.id): "absent"}
    w.db.commit()
    assert [deducted(w, m) for m in ms] == [0, 0, 0, 46_000, 46_000]                    # closed later: lifted for those it counted for


# ============================================================================================================ S3: a day off under a removed make-up (LESSONS 47)
def test_s3_removing_a_make_up_under_a_live_day_off_turns_the_original_into_a_holiday_and_the_void_gives_it_back(w):
    cancel_origin(w)
    mk = make_d2(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY).json()["id"]
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": ORIGIN_DAY, "dateTo": ORIGIN_DAY, "reason": "Хайит"})
    assert made.status_code == 201, made.text
    day_off = made.json()["dayOff"]["id"]
    assert row_of(w, w.G1, ORIGIN, ORIGIN_DAY).cancellation_kind == "centre"             # kept «по вине центра» while the make-up lives
    sheet(w, "admin", w.G1, mk, "2026-10-29")                                           # the make-up is removed
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.cancellation_kind, row.converted_from_kind, str(row.day_off_id), row.made_up_by) == ("holiday", "centre", day_off, None)
    w.db.expire_all()
    journal = w.db.query(LessonSubstitutionRequest).filter_by(group_id=w.G1.id, lesson_id=ORIGIN, lesson_date=ORIGIN_DAY, closed_reason="KIND_CHANGED").count()
    assert journal == 1                                                                 # the record of «Замены» (the money moved: no silent change)
    voided = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void")
    assert voided.status_code == 200, voided.text
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.cancellation_kind, row.converted_from_kind, row.day_off_id) == ("centre", None, None)      # what the action changed, its cancel gives back


def test_s3_removing_a_make_up_without_a_day_off_leaves_the_kind(w):
    cancel_origin(w)
    mk = make_d2(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY).json()["id"]
    sheet(w, "admin", w.G1, mk, "2026-10-29")
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.cancellation_kind, row.converted_from_kind, row.day_off_id) == ("centre", None, None)


# ============================================================================================================ S4 / S5: the words of the sheet
def test_s4_the_cell_of_a_not_decided_or_unpaid_lesson_is_zero_and_the_legend_names_both():
    from src.auth import payroll_engine as engine
    from src.auth.payroll_export import LEGEND_HEAD, legend_line

    day = date(2026, 10, 29)
    marks = {state: engine.Mark(day=day, lesson_id="l", state=state).mark for state in engine.STATES}
    assert marks[engine.UNDECIDED] == marks[engine.NO_PAY] == marks[engine.EXCUSED_CONFIRMED] == 0
    assert marks[engine.COUNTED] == marks[engine.UNPAID] == marks[engine.EXCUSED_PENDING] == 1 and marks[engine.NOT_IN_GROUP] is None
    assert "не решено" in LEGEND_HEAD and "без оплаты" in LEGEND_HEAD
    assert "не решено" in legend_line(True) and "без оплаты" in legend_line(False)


# ============================================================================================================ S7: no default role (LESSONS 17)
def test_s7_no_membership_is_a_403_and_the_decision_is_for_an_owner_or_an_admin_only(w):
    from src.auth.one_off_purpose import OneOffPurpose

    purposes = OneOffPurpose(w.svc)
    assert [purposes.actor_role(w.org.id, u.id) for u in (w.owner, w.admin, w.A)] == ["owner", "admin", "teacher"]
    with pytest.raises(OpsError) as stranger:
        purposes.actor_role(w.org.id, uuid4())
    assert (stranger.value.status_code, code_of(stranger.value)) == (403, "NOT_A_MEMBER")
    with pytest.raises(OpsError) as foreign:
        purposes.actor_role(w.org.id, w.foreign_admin.id)                                # an administrator of ANOTHER centre
    assert foreign.value.status_code == 403
    extra = make_d2(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="extra", money="none").json()["id"]
    with pytest.raises(OpsError) as teacher:
        purposes.decide(w.org.id, w.A.id, "teacher", extra, "pay", owner=False)
    assert (teacher.value.status_code, code_of(teacher.value)) == (403, "DECISION_FORBIDDEN")
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(id=extra).one().money == "none"


# ============================================================================================================ S8: the reason of a cancellation for a teacher
def test_s8_the_teacher_of_the_group_sees_the_reason_the_journal_shows_him(w):
    cancel_origin(w, reason="Преподаватель заболел")
    listed = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").json()
    journal = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).json()
    shown = journal["cancellationReasons"][f"{ORIGIN_DAY}_{ORIGIN}"]
    assert shown == "Преподаватель заболел" and listed["items"][0]["reason"] == shown      # the same reason in both places


def test_s8_the_criterion_is_the_journals_for_a_teacher_who_does_not_lead_the_group(w, clock):
    from src.auth.attendance_rules import AttendanceRules

    clock.set("2026-10-21 14:00")
    see = AttendanceRules.teacher_sees_lesson
    assert see(w.G1, w.A.id, None, "2026-01-01")                                         # the teacher of the group: every lesson
    assert see(w.G1, w.B.id, w.B.id, "2026-10-20")                                       # another teacher: his own lesson ...
    assert not see(w.G1, w.B.id, w.A.id, "2026-10-26")                                   # ... not the lesson of somebody else
    assert not see(w.G1, w.B.id, None, "2026-10-26")
    assert not see(w.G1, w.B.id, w.B.id, "2026-09-01")                                   # ... and not older than the window of the teacher (today - 7)
    assert client(w, "B").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").status_code == 403      # and the list itself is the group teacher's


# ============================================================================================================ S9: the overlaps of an additional lesson
def test_s9_an_additional_lesson_refuses_the_group_and_the_teacher_busy_and_warns_about_a_student(w):
    first = make_d2(w, "admin", w.G1, "2026-10-29", start="15:00", end="16:00", purpose="extra", money="none").json()["id"]
    r = make_d2(w, "admin", w.G1, "2026-10-29", start="15:30", end="16:30", purpose="extra", money="none", expect=409)             # a second click / another window
    assert code(r) == "GROUP_BUSY" and r.json()["detail"]["conflicts"]
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1
    # the lesson of the group on Wednesday 28.10 16:00-17:30
    assert code(make_d2(w, "admin", w.G1, "2026-10-28", start="17:00", end="18:00", purpose="extra", money="none", expect=409)) == "GROUP_BUSY"
    # A teaches G2 on Tuesday 27.10 10:00-11:00
    assert code(make_d2(w, "A", w.G1, "2026-10-27", start="10:30", end="11:30", purpose="extra", expect=409)) == "TEACHER_BUSY"
    # a student of G1 who has another lesson then: a warning, never a block (the same as a make-up)
    from src.models.auth import StudentGroup
    from tests.ops_helpers import open_period

    membership = StudentGroup(student_id=w.s6.id, group_id=w.G1.id)
    open_period(membership, datetime(2026, 9, 1, 5, 0))
    w.db.add(membership)
    w.db.commit()
    make_d2(w, "admin", w.G3, "2026-10-30", start="12:00", end="13:00", purpose="extra", money="none")
    ok = make_d2(w, "admin", w.G1, "2026-10-30", start="12:30", end="13:30", purpose="extra", money="none")
    assert [x["code"] for x in ok.json()["warnings"]] == ["STUDENT_BUSY"]
    assert one_off(w, first).purpose == "extra"


def test_s9_two_clicks_at_once_give_one_additional_lesson_and_one_group_busy(w):
    from src.auth.ops_schemas import CreateOneOffLessonRequestB

    def create(svc):
        req = CreateOneOffLessonRequestB(groupId=str(w.G1.id), lessonDate=date(2026, 10, 29), startTime="15:00", endTime="16:00", purpose="extra", money="none")
        return svc.create_one_off_lesson(str(w.org.id), req, actor_id=str(w.admin.id), is_org_admin=True)

    results = run_parallel(w, create, create)
    ok = [r for r, e in results if e is None]
    errors = [e for r, e in results if e is not None]
    assert len(ok) == 1 and [code_of(e) for e in errors] == ["GROUP_BUSY"], results
    w.db.expire_all()
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1


def test_s9_the_past_stays_refused(w):
    r = make_d2(w, "admin", w.G1, "2026-10-20", start="15:00", end="16:00", purpose="extra", money="none", expect=400)
    assert "прошедшую дату" in r.text and w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 0


# ============================================================================================================ S10
def test_s10_the_change_of_the_kind_and_the_creation_of_a_make_up_take_the_group_then_the_row(w):
    """The kind-changing road used to take the row and then (through the record of «Замены») a key share of the group, the creation took the group and then the row: a cycle."""
    from src.auth.cancellation_kind import ChangeCancellationKind, change_cancellation_kind
    from src.auth.ops_schemas import CreateOneOffLessonRequestB

    cancel_origin(w)

    def create(svc):
        req = CreateOneOffLessonRequestB(groupId=str(w.G1.id), lessonDate=date(2026, 10, 29), startTime="12:00", endTime="13:00", purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=date(2026, 10, 26))
        return svc.create_one_off_lesson(str(w.org.id), req, actor_id=str(w.admin.id), is_org_admin=True)

    def change(svc):
        body = ChangeCancellationKind(lessonDate=date(2026, 10, 26), lessonId=ORIGIN, cancellationKind="holiday", reason="Это праздник")
        return change_cancellation_kind(svc.db, str(w.org.id), str(w.G1.id), body, actor_id=str(w.admin.id))

    results = run_parallel(w, create, change)
    ok = [i for i, (r, e) in enumerate(results) if e is None]
    assert len(ok) == 1, results                                                         # exactly one road wins, the other is refused with a code (no deadlock, no 500)
    loser = results[1 - ok[0]][1]
    assert code_of(loser) in ("MAKEUP_EXISTS", "MAKEUP_NOT_ALLOWED"), loser
    w.db.expire_all()
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.cancellation_kind == "centre" and bool(row.made_up_by)) or (row.cancellation_kind == "holiday" and not row.made_up_by)


def test_s10_a_make_up_cannot_be_returned_into_a_slot_that_was_taken_meanwhile(w):
    cancel_origin(w)
    first = makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    sheet(w, "admin", w.G1, first, "2026-10-29")
    make_d2(w, "admin", w.G1, "2026-10-29", start="12:30", end="13:30", purpose="extra", money="none")          # the slot is taken by an additional lesson
    r = restore(w, "admin", w.G1, first, "2026-10-29", expect=409)
    assert code(r) == "GROUP_BUSY" and pointer(w) is None
    # the teacher is busy then (A teaches G2 on Tuesday 27.10 10:00-11:00)
    second = makeup(w, "admin", w.G1, "2026-10-30", start="12:00", end="13:00").json()["id"]
    sheet(w, "admin", w.G1, second, "2026-10-30")
    make_d2(w, "admin", w.G2, "2026-10-30", start="12:30", end="13:30", purpose="extra", money="none")
    assert code(restore(w, "admin", w.G1, second, "2026-10-30", expect=409)) == "TEACHER_BUSY" and pointer(w) is None


def test_s10_a_make_up_is_returned_when_the_slot_is_free_and_the_student_warning_comes_with_it(w):
    from src.models.auth import StudentGroup
    from tests.ops_helpers import open_period

    cancel_origin(w)
    first = makeup(w, "admin", w.G1, "2026-10-30", start="12:00", end="13:00").json()["id"]
    sheet(w, "admin", w.G1, first, "2026-10-30")
    membership = StudentGroup(student_id=w.s6.id, group_id=w.G1.id)
    open_period(membership, datetime(2026, 9, 1, 5, 0))
    w.db.add(membership)
    w.db.commit()
    make_d2(w, "admin", w.G3, "2026-10-30", start="12:30", end="13:30", purpose="extra", money="none")
    r = restore(w, "admin", w.G1, first, "2026-10-30")
    assert pointer(w) == first and [x["code"] for x in r.json()["warnings"]] == ["STUDENT_BUSY"]


# ============================================================================================================ the mini-R3
def test_s3_a_day_off_voided_before_the_make_up_is_removed_converts_nothing(w):
    cancel_origin(w)
    mk = make_d2(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY).json()["id"]
    day_off = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": ORIGIN_DAY, "dateTo": ORIGIN_DAY, "reason": "Хайит"}).json()["dayOff"]["id"]
    assert client(w, "owner").post(f"{API}/org/days-off/{day_off}/void").status_code == 200
    sheet(w, "admin", w.G1, mk, "2026-10-29")
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.cancellation_kind, row.converted_from_kind, row.day_off_id, row.made_up_by) == ("centre", None, None, None)


def test_s3_a_day_off_that_is_being_voided_right_now_is_not_waited_for_and_converts_nothing(w):
    from sqlalchemy.orm import sessionmaker

    from src.models.auth import OrgDayOff

    cancel_origin(w)
    mk = make_d2(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY).json()["id"]
    day_off = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": ORIGIN_DAY, "dateTo": ORIGIN_DAY, "reason": "Хайит"}).json()["dayOff"]["id"]
    other = sessionmaker(w.db.get_bind())()
    try:
        other.query(OrgDayOff).filter(OrgDayOff.id == day_off).with_for_update().first()             # «Отменить выходной» is in the middle of its work
        sheet(w, "admin", w.G1, mk, "2026-10-29")                                                     # no wait, no deadlock, nothing converted
        row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
        assert (row.cancellation_kind, row.day_off_id, row.made_up_by) == ("centre", None, None)
    finally:
        other.rollback()
        other.close()


def test_a_copy_of_a_copy_of_a_moved_regular_lesson_still_deducts_when_the_centre_cancels_it(pg_session, clock):
    w = build_org(pg_session)
    g, students, ms = five(w, clock)
    first = move_money(w, g, "lesson-wed", "2026-10-28", "2026-10-29")
    second = move_money(w, g, first, "2026-10-29", "2026-10-30")
    assert one_off(w, second).purpose == "moved"
    cancel(w, g, second, "2026-10-30")
    assert [deducted(w, m) for m in ms] == [92_000] * 5                                # the Monday lesson and the last copy (a lesson of the price of the month)


def test_undecided_changes_neither_the_total_of_the_sheet_nor_the_total_of_the_excel(pg_session, clock):
    import io

    from openpyxl import load_workbook

    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")

    def excel_totals(sheet_id):
        out = client(w, "owner").get(f"{API}/org/payroll/sheets/{sheet_id}/export.xlsx")
        assert out.status_code == 200
        rows = [[c.value for c in row if c.value is not None] for ws in load_workbook(io.BytesIO(out.content)).worksheets for row in ws.iter_rows()]
        return [r for r in rows if r and r[0] == "Итого"], [r for r in rows if r and isinstance(r[0], str) and r[0].startswith("Заработано ")]

    s = pay_sheet(w, "2026-10")
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "expected_fingerprint": s["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    excel, head = excel_totals(saved.json()["id"])
    assert s["total_amount"] == 0 and excel == [["Итого", 0, 0, 0]] and head and head[0][0].startswith("Заработано 0 ·")      # five students «не решено»: nothing in the sum, nothing in the file
    decide(w, "owner", made, "skip")
    look = client(w, "owner").get(f"{API}/org/payroll/sheets/{saved.json()['id']}/diff").json()
    patched = client(w, "owner").patch(f"{API}/org/payroll/sheets/{saved.json()['id']}", json={"revision": saved.json()["revision"], "recalculate": True, "expected_fingerprint": look["source_fingerprint"]})
    assert patched.status_code == 200, patched.text
    excel, _ = excel_totals(saved.json()["id"])
    assert pay_sheet(w, "2026-10")["total_amount"] == 0 and excel == [["Итого", 0, 0, 0]]                      # «без оплаты» is 0 as well


def test_the_doors_of_the_lists_and_the_decision_over_http(w):
    from tests.pg_world import make_client

    cancel_origin(w)
    extra = make_d2(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="extra", money="wait").json()["id"]
    url = f"{API}/org/groups/{w.G1.id}/makeup-candidates"
    for role in ("student", "parent"):
        assert make_client(w, user=w.A, org="own", org_role=role, role=role).get(url).status_code == 403, role
    assert client(w, "B").get(url).status_code == 403                                      # a teacher of another group
    assert client(w, "A").get(url).status_code == 200
    # a teacher cannot decide: the door of the road is the owner / administrator (the guard DECISION_FORBIDDEN of the service sits behind it)
    r = client(w, "A").post(f"{API}/org/one-off-lessons/{extra}/money-decision", json={"decision": "pay"})
    assert r.status_code == 403
    assert client(w, "B").post(f"{API}/org/one-off-lessons/{extra}/money-decision", json={"decision": "pay"}).status_code == 403
    assert one_off(w, extra).money == "wait"


def test_a_cancelled_additional_lesson_is_not_returned_into_a_slot_that_was_taken_meanwhile(w):
    first = make_d2(w, "admin", w.G1, "2026-10-29", start="15:00", end="16:00", purpose="extra", money="none").json()["id"]
    sheet(w, "admin", w.G1, first, "2026-10-29")
    make_d2(w, "admin", w.G1, "2026-10-29", start="15:30", end="16:30", purpose="extra", money="none")
    r = restore(w, "admin", w.G1, first, "2026-10-29", expect=409)
    assert code(r) == "GROUP_BUSY"
    assert one_off(w, first).purpose == "extra" and row_of(w, w.G1, first, "2026-10-29").status == "cancelled"
