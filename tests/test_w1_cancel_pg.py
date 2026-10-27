"""W1 S2 / S5 / S6 / S7 / S11: THE cancellation of one lesson and its undo (PostgreSQL).

Sources: lesson-move-cancel-dayoff.md Д1 / Д3 / Д8 / Д12 / Д13 / Д19 / Д20 / Д22 / Д27 and §8 №11-14, 18-22, 28; substitutions.md D8 / D12 / D18 / D20;
the invariants И1 (one decision - one road - one record), И3 (no cancelled lesson keeps a live substitute), И5 (a repeat never overwrites the reason), И6."""
from datetime import date

import pytest

from src.auth.ops_rules import OpsError
from src.auth.schemas import SaveAttendanceRequest
from src.models.auth import (
    GroupAttendance,
    GroupAttendanceAudit,
    GroupAudit,
    LessonSubstitutionEvent,
    LessonSubstitutionRequest as Req,
    LessonTeacherAssignment,
    OrgDayOff,
)
from tests.b2_helpers import API, build_d2, clock, decide, events_of, exams, needs_pg, pg_session, req, requests_of, row_of, run_parallel  # noqa: F401  (fixtures)
from tests.ops_helpers import client
from tests.test_ops_b2_decide_pg import L, Sync, one  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def sheet(w, who, group, lesson, day, reason="Преподаватель болен", expect=200, **extra):
    """The lesson sheet / the teacher's button: POST attendance, status cancelled."""
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance",
                            json={"lessonDate": day, "lessonId": lesson, "status": "cancelled", "records": {}, "cancellationReason": reason, **extra})
    assert r.status_code == expect, r.text
    return r


def restore(w, who, group, lesson, day, reason="Отменили по ошибке", expect=200):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/lessons/restore", json={"lessonId": lesson, "lessonDate": day, "reason": reason})
    assert r.status_code == expect, r.text
    return r


def code(r):
    return r.json()["detail"]["code"]


def cancelled_reqs(w, group=None):
    return [r for r in requests_of(w) if r.status == "cancelled" and (group is None or r.group_id == group.id)]


def trace(w, group, lesson, day):
    """What a cancellation leaves: the row, its audit, the record of the journal (shape only - no ids)."""
    w.db.expire_all()
    row = row_of(w, group, lesson, day)
    audits = w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).all()
    rec = w.db.query(Req).filter_by(group_id=group.id, lesson_id=lesson, lesson_date=day).one()
    evs = events_of(w, rec.id)
    return {"row": (row.status, row.cancellation_kind, row.version, row.records, row.teacher_id, row.day_off_id),
            "audit": sorted(a.action for a in audits), "record": (rec.status, rec.requester_id, rec.decided_via, rec.closed_reason),
            "events": [e.action for e in evs]}


def notices(w, student, month):
    return client(w, "admin").get(f"{API}/org/students/{student.id}/attendance", params={"month": month}).json()["notices"]


# ================================================================================================ S2: one road, one trace
def test_s2_the_lesson_sheet_leaves_the_trace_of_the_decision_of_a_request(w):
    """§8 №13-14: the same cancellation through a request and from the sheet -> the same row, audit, record of «Замены», note of the student (Д1, И1)."""
    item = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен").status_code == 200
    sheet(w, "admin", w.G1, "lesson-l1", "2026-11-09")
    by_request, by_sheet = trace(w, w.G1, "lesson-l1", "2026-11-02"), trace(w, w.G1, "lesson-l1", "2026-11-09")
    assert by_request == by_sheet
    assert by_sheet["row"][:4] == ("cancelled", "centre", 1, {}) and by_sheet["audit"] == ["cancelled"] and by_sheet["events"] == ["requested", "cancelled"]
    # the record of the sheet says who made it: origin system, trigger manager_cancel, the author, the main teacher of the lesson
    rec = w.db.query(Req).filter_by(group_id=w.G1.id, lesson_date="2026-11-09").one()
    assert (rec.origin, rec.requester_id, rec.decided_by, rec.decided_via, rec.decision_note) == ("system", w.A.id, w.admin.id, "request", "Преподаватель болен")
    assert events_of(w, rec.id)[0].snapshot["trigger"] == "manager_cancel"
    # the journal «Замены» shows both; the card of the student shows both notes (no reason)
    listed = client(w, "admin").get(SUB, params={"status": "decided"}).json()["items"]
    assert sorted(i["lesson"]["date"] for i in listed) == ["2026-11-02", "2026-11-09"] and {i["status"] for i in listed} == {"cancelled"}
    assert [(n["date"], n["kind"]) for n in notices(w, w.s1, "2026-11")] == [("2026-11-02", "cancelled"), ("2026-11-09", "cancelled")]
    assert "болен" not in str(notices(w, w.s1, "2026-11"))


def test_s2_a_substitute_set_on_the_lesson_is_released_by_every_cancellation(w):
    """Д12 / D8 / И3: sheet cancel and decision both take the substitute off (event ``assignment_cleared``, trigger ``cancel``); the substitute's «Замена» is gone."""
    put = lambda lesson, day, teacher: client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher",
                                                              json={"lessonDate": day, "lessonId": lesson, "teacherId": str(teacher.id), "reason": "Болезнь"})
    assert put("lesson-l1", "2026-10-26", w.D).status_code == 200
    r = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26")
    assert [x["code"] for x in r.json()["warnings"]] == ["SUBSTITUTE_RELEASED"] and "Замена снята" in r.json()["warnings"][0]["message"]
    w.db.expire_all()
    asg = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-10-26").one()
    assert asg.cancelled_at is not None and asg.cancelled_by == w.admin.id
    cleared = [e for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="assignment_cleared")]
    assert [e.snapshot["trigger"] for e in cleared] == ["cancel"]
    assert client(w, "D").get(f"{SUB}/mine").json()["assignedToMe"] == []
    # the decision of a request on another lesson: the same (the substitute D asks to be replaced himself - a request of the lesson that has a substitute)
    assert put("lesson-l1", "2026-11-09", w.D).status_code == 200
    item = one(w, "D", "L1", "2026-11-09")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен").status_code == 200
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-11-09").one().cancelled_at is not None


def test_s2_a_waiting_request_is_decided_by_the_direct_cancel(w):
    """§8 №18: a pending request + a cancellation from the sheet = ONE record, decided ``cancelled`` (not a closed ``LESSON_CANCELLED`` next to a second record)."""
    item = one(w, "A", "L1", "2026-10-26")
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Решили отменить")
    (rec,) = requests_of(w)
    assert (rec.id, str(rec.id) == item["id"], rec.status, rec.decided_by, rec.decided_via, rec.closed_reason, rec.decision_note) == (rec.id, True, "cancelled", w.admin.id, "request", None, "Решили отменить")
    assert [e.action for e in events_of(w, rec.id)] == ["requested", "cancelled"]


def test_s2_a_copy_of_a_move_cancelled_from_the_sheet_leaves_its_own_note(w):
    """Д22: move, then cancel the copy: the copy has its own record and its own note (the original still says «перенесён»)."""
    moved = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "newDate": "2026-10-28",
                                                                                   "startTime": "12:00", "endTime": "13:30", "reason": "Перенос"})
    assert moved.status_code == 200, moved.text
    copy_id = moved.json()["result"]["newLesson"]["id"]
    sheet(w, "admin", w.G1, copy_id, "2026-10-28", reason="Не будет и на новой дате")
    w.db.expire_all()
    recs = sorted((r.lesson_id, r.status) for r in requests_of(w))
    assert recs == sorted([("lesson-l1", "rescheduled"), (copy_id, "cancelled")])
    kinds = sorted((n["lessonId"], n["kind"]) for n in notices(w, w.s1, "2026-10"))
    assert (copy_id, "cancelled") in kinds and ("lesson-l1", "rescheduled") in kinds


def test_s2_bulk_and_single_decisions_are_the_same_cancellation_with_the_chosen_kind(w):
    a, b = one(w, "A", "L1", "2026-11-02"), one(w, "A", "L1", "2026-11-09")
    r = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": [a["id"], b["id"]], "decision": "cancel", "reason": "Курсы", "cancellationKind": "holiday"})
    assert r.status_code == 200 and r.json()["updated"] == 2, r.text
    assert {row_of(w, w.G1, "lesson-l1", d).cancellation_kind for d in ("2026-11-02", "2026-11-09")} == {"holiday"}
    assert all(x.status == "cancelled" for x in requests_of(w))


# ===================================================================================== S2: a date of a live day off is a holiday
def test_s2_every_cancellation_of_a_date_under_a_live_day_off_is_a_holiday_with_the_day_off(w, sync):
    """Д19: day off -> the exam blocks the lesson -> the administrator cancels it by hand as «по вине центра» -> it is a holiday of THAT day off (no deduction)."""
    sync.mode = "live"
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    assert made.status_code == 201 and [x["lessonId"] for x in made.json()["blocked"]] == ["lesson-l2"], made.text
    day_off = made.json()["dayOff"]["id"]
    sync.mode = "ok"
    r = sheet(w, "admin", w.G1, "lesson-l2", "2026-10-28")                              # the form sends no kind: «по вине центра»
    row = row_of(w, w.G1, "lesson-l2", "2026-10-28")
    assert (row.cancellation_kind, str(row.day_off_id)) == ("holiday", day_off)
    assert [x["code"] for x in r.json()["warnings"]] == ["CANCELLED_AS_HOLIDAY"] and "Хайит" in r.json()["warnings"][0]["message"]
    assert requests_of(w) == []                                                         # the day off is its journal: no second record
    # a day off that is voided gives it back like its own lessons
    voided = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void")
    assert voided.status_code == 200 and [x["lessonId"] for x in voided.json()["restored"]] == ["lesson-l2"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None


def test_s2_the_lesson_was_not_held_of_a_day_off_date_is_a_holiday_too(w):
    """A past lesson that the day off did not cancel (it only warns: Д23) is closed by the teacher as «Урока не было» -> holiday, no deduction (A2 of the summary)."""
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-20", "dateTo": "2026-10-20", "reason": "Хайит"})
    assert made.status_code == 201
    assert [x["lessonId"] for x in made.json()["blocked"]] == ["lesson-l3"] and made.json()["cancelled"] == []
    r = client(w, "A").post(f"{API}/org/groups/{w.G2.id}/attendance/reconcile/cancel", json={"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "cancellationReason": "Урока не было"})
    assert r.status_code == 200 and [x["code"] for x in r.json()["warnings"]] == ["CANCELLED_AS_HOLIDAY"]
    row = row_of(w, w.G2, "lesson-l3", "2026-10-20")
    assert (row.cancellation_kind, str(row.day_off_id)) == ("holiday", made.json()["dayOff"]["id"])


def test_s2_an_explicit_holiday_on_a_day_off_date_has_no_warning(w, sync):
    sync.mode = "live"
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    sync.mode = "ok"
    r = sheet(w, "owner", w.G1, "lesson-l2", "2026-10-28", cancellationKind="holiday")
    assert r.json()["warnings"] == [] and str(row_of(w, w.G1, "lesson-l2", "2026-10-28").day_off_id) == made.json()["dayOff"]["id"]


# ======================================================================================= S6: a repeat never rewrites the past
def test_s6_an_identical_repeat_is_a_200_and_a_different_one_is_refused_with_the_reason_intact(w):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Преподаватель болен")
    again = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Преподаватель болен")
    assert again.json()["version"] == 1 and w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-26").count() == 1
    other = sheet(w, "owner", w.G1, "lesson-l1", "2026-10-26", reason="Совсем другая причина", expect=409)
    assert code(other) == "ALREADY_CANCELLED"
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.cancellation_reason, row.version, row.marked_by) == ("Преподаватель болен", 1, w.admin.id)
    assert len(requests_of(w)) == 1                                                       # no second record


def test_s6_a_moved_original_is_not_cancelled_again_and_keeps_its_moved_to_text(w):
    moved = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "newDate": "2026-10-28",
                                                                                   "startTime": "12:00", "endTime": "13:30", "reason": "Перенос"})
    assert moved.status_code == 200
    before = row_of(w, w.G1, "lesson-l1", "2026-10-26").cancellation_reason
    r = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Другая причина", expect=409)
    assert code(r) == "ALREADY_MOVED" and "перенесён на 28.10" in r.json()["detail"]["message"]
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert row.cancellation_reason == before and row.cancellation_kind is None and row.version == 1


def test_s6_a_decision_after_the_lesson_was_cancelled_by_the_sheet_is_a_409_with_the_record(w):
    item = one(w, "A", "L1", "2026-10-26")
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Решили сами")
    r = decide(w, "owner", item["id"], decision="cancel", reason="Другая причина")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").cancellation_reason == "Решили сами"


def _cancel_call(w, reason):
    def call(svc):
        out = svc.save_group_attendance(str(w.org.id), str(w.G1.id), SaveAttendanceRequest(lessonDate=date(2026, 10, 26), lessonId="lesson-l1", status="cancelled",
                                                                                           cancellationReason=reason), marked_by=str(w.admin.id), is_org_admin=True, is_org_manager=True)
        return out["version"]
    return call


def test_s6_two_cancellations_at_once_are_one_cancellation_and_a_409_never_a_500(w):
    w.db.commit()
    (a, ea), (b, eb) = run_parallel(w, _cancel_call(w, "Первая причина"), _cancel_call(w, "Вторая причина"))
    errors = [e for e in (ea, eb) if e is not None]
    assert len(errors) == 1 and isinstance(errors[0], OpsError) and errors[0].code == "ALREADY_CANCELLED", (ea, eb)
    w.db.expire_all()
    rows = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-26").all()
    assert len(rows) == 1 and rows[0].version == 1 and rows[0].cancellation_reason in ("Первая причина", "Вторая причина")


def test_s6_a_move_and_a_cancellation_of_one_lesson_at_once_leave_one_outcome_and_no_500(w):
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.substitution_schemas import LessonMoveRequest

    def move(svc):
        return Substitutions(svc).move_lesson(w.org.id, w.admin.id, "admin", w.G1.id, LessonMoveRequest(
            lessonId="lesson-l1", lessonDate=date(2026, 10, 26), newDate=date(2026, 10, 28), startTime="12:00", endTime="13:30", reason="Перенос"))

    w.db.commit()
    results = run_parallel(w, move, _cancel_call(w, "Отмена"))
    errors = [e for _, e in results if e is not None]
    assert all(isinstance(e, OpsError) and e.status_code == 409 for e in errors), results                # a refusal of the loser, never an IntegrityError
    assert len(errors) <= 1
    w.db.expire_all()
    assert w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-26").count() == 1


# =============================================================================================== S7: marks and started lessons
def start_and_mark(w, marks=True):
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})
    assert r.status_code == 200, r.text
    if marks:
        saved = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={
            "lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress", "records": {str(w.s1.id): "present"}})
        assert saved.status_code == 200, saved.text


def test_s7_a_started_lesson_with_marks_is_not_cancelled_without_marks_it_is(w, clock):
    """Д27: cancelling a started lesson would erase the marks and the reviewed excuses - refused until the marks are taken off; with none it is a cancellation."""
    clock.set("2026-10-26 10:30")
    start_and_mark(w)
    r = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", expect=409)
    assert code(r) == "LESSON_HAS_MARKS" and "Сначала снимите отметки" in r.json()["detail"]["message"]
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.status, row.records, row.version) == ("in_progress", {str(w.s1.id): "present"}, 2)
    assert requests_of(w) == []                                                         # the refusal left no record behind
    # the marks are taken off (a save of an empty snapshot) -> the cancellation goes through
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={
        "lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress", "records": {}, "expectedVersion": 2}).status_code == 200
    ok = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Урок не состоялся")
    assert ok.json()["version"] == 4
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.status, row.cancellation_kind, row.records, row.cancellation_reason, row.completed_at) == ("cancelled", "centre", {}, "Урок не состоялся", None)
    assert sorted(a.action for a in w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id)) == ["cancelled", "started", "updated", "updated"]
    assert [x.status for x in requests_of(w)] == ["cancelled"]


def test_s7_a_finished_lesson_is_not_cancelled(w):
    r = sheet(w, "admin", w.G1, "lesson-l1", "2026-10-19", expect=409)                  # held and completed in D0
    assert code(r) == "LESSON_ALREADY_STARTED"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-19").status == "completed"


def test_s7_the_teacher_of_a_lesson_that_is_going_on_is_not_changed_but_a_finished_one_may_be_corrected(w, clock):
    """D20: «Заменить преподавателя» on a started lesson is refused; the finished lesson of the past keeps «назначить того, кто провёл»."""
    clock.set("2026-10-26 10:30")
    start_and_mark(w, marks=False)
    put = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    assert put.status_code == 409 and code(put) == "LESSON_IN_PROGRESS"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").teacher_id == w.A.id
    done = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-19", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Провёл D"})
    assert done.status_code == 200 and done.json()["teacherId"] == str(w.D.id), done.text


# ============================================================================================ S11: the kind in the decision
def test_s11_the_decision_cancel_takes_the_kind_the_default_is_the_centres_fault(w):
    a, b = one(w, "A", "L1", "2026-11-02"), one(w, "A", "L1", "2026-11-09")
    ra = decide(w, "admin", a["id"], decision="cancel", reason="Преподаватель болен")
    rb = decide(w, "admin", b["id"], decision="cancel", reason="Праздник у учеников", cancellationKind="holiday")
    assert ra.json()["result"] == {**ra.json()["result"], "kind": "cancel", "cancellationKind": "centre", "warnings": []}
    assert rb.json()["result"]["cancellationKind"] == "holiday"
    assert (row_of(w, w.G1, "lesson-l1", "2026-11-02").cancellation_kind, row_of(w, w.G1, "lesson-l1", "2026-11-09").cancellation_kind) == ("centre", "holiday")
    bad = decide(w, "admin", one(w, "A", "L1", "2026-11-16")["id"], decision="cancel", reason="Причина", cancellationKind="whatever")
    assert bad.status_code == 422


def test_s11_a_cancel_decision_on_a_day_off_date_is_a_holiday_with_the_warning(w, sync):
    sync.mode = "live"
    client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    sync.mode = "ok"
    item = one(w, "A", "L2", "2026-10-28")
    r = decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен")
    assert r.status_code == 200 and r.json()["result"]["cancellationKind"] == "holiday" and [x["code"] for x in r.json()["result"]["warnings"]] == ["CANCELLED_AS_HOLIDAY"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").day_off_id is not None
    assert [x.status for x in requests_of(w)] == ["cancelled"]               # the request that was asked for is still decided


def test_s11_the_decision_reschedule_keeps_the_room_of_the_lesson(w):
    """D5 / Д29: the move through «Замены» and the move from the lesson sheet give the same new lesson (the room is not lost)."""
    w.G1.schedule = [dict(slot, room="Каб. 12") for slot in w.G1.schedule]
    w.db.commit()
    item = one(w, "A", "L1", "2026-10-26")
    r = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-28", startTime="12:00", endTime="13:30", reason="Перенос")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["newLesson"]["room"] == "Каб. 12"
    assert client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-11-02", "newDate": "2026-11-04", "startTime": "12:00",
                                                                                   "endTime": "13:30", "reason": "Перенос", "room": "Каб. 12"}).json()["result"]["newLesson"]["room"] == "Каб. 12"
    explicit = decide(w, "admin", one(w, "A", "L1", "2026-11-09")["id"], decision="reschedule", newDate="2026-11-11", startTime="12:00", endTime="13:30", reason="Перенос", room="Каб. 5")
    assert explicit.json()["result"]["newLesson"]["room"] == "Каб. 5"


# ================================================================================================= S5: give a cancelled lesson back
def test_s5_a_cancelled_future_lesson_comes_back_with_the_trace_and_the_journal_entry(w, clock):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Ошибка администратора")
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is not None
    clock.set("2026-10-21 14:01")                                      # the records of a lesson are ordered by their moment: a frozen clock would tie them
    out = restore(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Отменили по ошибке")
    assert out.json() == {**out.json(), "ok": True, "restored": True, "lessonId": "lesson-l1", "lessonDate": "2026-10-26", "previousKind": "centre", "warnings": []}
    assert "назначьте их заново" in out.json()["examsNote"]
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None                           # the lesson is scheduled again
    occ = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json()
    lesson = next(o for o in occ if o["id"] == "lesson-l1")
    assert lesson.get("substitutionOutcome") is None                                    # «Отменён» no longer shows
    # recorded BEFORE the row was deleted: the group audit and the journal «Замены»
    w.db.expire_all()
    (audit,) = w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_restored").all()
    assert audit.actor_id == w.admin.id and audit.snapshot["cancellationKind"] == "centre" and audit.snapshot["cancellationReason"] == "Ошибка администратора"
    assert audit.snapshot["reason"] == "Отменили по ошибке" and audit.snapshot["cancelledBy"]
    recs = sorted(requests_of(w), key=lambda r: r.created_at)
    assert [(r.status, r.closed_reason, r.decided_via) for r in recs] == [("cancelled", None, "request"), ("closed", "LESSON_RESTORED", "manager_restore")]
    assert events_of(w, recs[1].id)[-1].action == "closed" and events_of(w, recs[1].id)[-1].snapshot["previous"]["cancellationKind"] == "centre"
    listed = client(w, "admin").get(SUB, params={"status": "decided"}).json()["items"]
    assert any(i["decision"]["closedReason"] == "LESSON_RESTORED" and i["trigger"] == "manager_restore" and i["decision"]["closedReasonText"] == "Урок возвращён" for i in listed)
    assert notices(w, w.s1, "2026-10") == []                                            # the student sees nothing about a lesson that is on
    # it can be cancelled again - and only the NEWEST outcome counts
    clock.set("2026-10-21 14:02")
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Теперь точно")
    assert [n["kind"] for n in notices(w, w.s1, "2026-10")] == ["cancelled"]
    assert restore(w, "admin", w.G1, "lesson-l1", "2026-10-26").status_code == 200
    assert restore(w, "admin", w.G1, "lesson-l1", "2026-10-26", expect=409).json()["detail"]["code"] == "RESTORE_NOT_CANCELLED"


def test_s5_only_the_owner_and_the_admin_and_only_with_a_reason_of_their_centre(w):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/lessons/restore", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "reason": "Верните"}).status_code == 403
    assert restore(w, "owner", w.G1, "lesson-l1", "2026-10-26", reason="ab", expect=422).json()["detail"]["code"] == "REASON_REQUIRED"
    assert restore(w, "owner", w.G1, "lesson-l1", "2026-10-26", reason="   ", expect=422).json()["detail"]["code"] == "REASON_REQUIRED"
    from tests.pg_world import make_client
    foreign = make_client(w, user=w.foreign_admin, org="other", org_role="admin", role="admin")
    assert foreign.post(f"{API}/org/groups/{w.G1.id}/lessons/restore", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "reason": "Верните"}).status_code == 404
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is not None                       # nothing was given back


def test_s5_not_cancelled_and_a_moved_original_are_refused_with_their_own_text(w):
    assert restore(w, "admin", w.G1, "lesson-l1", "2026-10-26", expect=409).json()["detail"]["code"] == "RESTORE_NOT_CANCELLED"
    client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "newDate": "2026-10-28", "startTime": "12:00",
                                                                           "endTime": "13:30", "reason": "Перенос"})
    r = restore(w, "admin", w.G1, "lesson-l1", "2026-10-26", expect=409)
    assert r.json()["detail"]["code"] == "RESTORE_MOVED" and "28.10" in r.json()["detail"]["message"]
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").status == "cancelled"


def test_s5_a_lesson_that_should_have_started_is_not_given_back(w, clock):
    sheet(w, "admin", w.G1, "lesson-l2", "2026-10-21", reason="Заболел")              # today 16:00, now 14:00
    clock.set("2026-10-21 16:30")
    assert restore(w, "admin", w.G1, "lesson-l2", "2026-10-21", expect=409).json()["detail"]["code"] == "RESTORE_PAST"
    reconcile = client(w, "admin").post(f"{API}/org/groups/{w.G2.id}/attendance/reconcile/cancel", json={"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "cancellationReason": "Не было"})
    assert reconcile.status_code == 200
    assert restore(w, "admin", w.G2, "lesson-l3", "2026-10-20", expect=409).json()["detail"]["code"] == "RESTORE_PAST"


def test_s5_a_lesson_of_a_day_off_is_not_given_back_one_by_one(w):
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    assert [x["lessonId"] for x in made.json()["cancelled"]] == ["lesson-l2"]
    r = restore(w, "admin", w.G1, "lesson-l2", "2026-10-28", expect=409)
    assert r.json()["detail"]["code"] == "RESTORE_DAY_OFF" and "Хайит" in r.json()["detail"]["message"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"


def test_s5_a_lesson_the_schedule_no_longer_has_and_a_teacher_who_became_busy_are_refused(w):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-11-09")
    # the series moved to Friday from 03.11: the 9th (a Monday) is no lesson any more
    assert client(w, "admin").put(f"{API}/org/schedule/lessons/lesson-l1/recurrence", json={"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "reason": "День"}).status_code == 200
    assert restore(w, "admin", w.G1, "lesson-l1", "2026-11-09", expect=409).json()["detail"]["code"] == "RESTORE_LESSON_GONE"
    # the teacher got another lesson in the slot meanwhile
    sheet(w, "admin", w.G1, "lesson-l2", "2026-10-28")
    one_off = client(w, "admin").post(f"{API}/org/schedule/occurrences", json={"groupId": str(w.G2.id), "lessonDate": "2026-10-28", "startTime": "16:30", "endTime": "17:00"})
    assert one_off.status_code == 201, one_off.text
    busy = restore(w, "admin", w.G1, "lesson-l2", "2026-10-28", expect=409)
    assert busy.json()["detail"]["code"] == "TEACHER_BUSY"


def test_s5_a_lesson_of_an_archived_group_is_not_given_back(w):
    from tests.b2_helpers import new_group, new_slot

    empty = new_group(w.db, w.org, w.ids, "E", "Пустая", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-28")])
    w.db.commit()
    sheet(w, "admin", empty, "lesson-e", "2026-10-28")
    assert client(w, "admin").post(f"{API}/org/groups/{empty.id}/archive", json={"reason": "Курс закончен"}).status_code == 200
    assert restore(w, "admin", empty, "lesson-e", "2026-10-28", expect=409).json()["detail"]["code"] == "GROUP_ARCHIVED"
