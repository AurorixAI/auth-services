"""W1 S4 / S12: moving a lesson (PostgreSQL).

S4: a teacher whose request the administrator REJECTED does not move that lesson himself (D16).  S12: a lesson that has a substitute keeps him when it is moved and it is HIS time
that is checked (D19); a student who has another lesson at the new time is a warning ``STUDENT_BUSY``, never a block (D30, decision 4).
Sources: substitutions.md D16 / D19 / INV-8; lesson-move-cancel-dayoff.md Д30 and §8 №3-12; WAVE1_SUMMARY.md decisions 1 and 4."""
from datetime import datetime

import pytest

from src.models.auth import GroupAttendance, LessonTeacherAssignment, StudentGroup
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client, new_group, new_slot
from tests.test_ops_b2_decide_pg import Sync, one  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def move(w, who, group, lesson, day, new_day, start, end, expect=200, **extra):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/lesson-move", json={"lessonId": lesson, "lessonDate": day, "newDate": new_day, "startTime": start, "endTime": end,
                                                                             "reason": "Перенос", **extra})
    assert r.status_code == expect, r.text
    return r


def check(w, who, group, lesson, day, new_day, start, end):
    r = client(w, who).get(f"{API}/org/groups/{group.id}/lesson-move-check", params={"lessonId": lesson, "lessonDate": day, "newDate": new_day, "startTime": start, "endTime": end})
    assert r.status_code == 200, r.text
    return r.json()


# ===================================================================================================== S4
def test_s4_after_a_rejection_the_teacher_does_not_move_the_lesson_himself(w):
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "admin", item["id"], decision="reject", reason="В этот день экзамен").status_code == 200
    r = move(w, "A", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30", expect=409)
    assert r.json()["detail"]["code"] == "REQUEST_REJECTED" and r.json()["detail"]["message"] == "Администратор отклонил заявку по этому уроку"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None                                           # nothing was written
    # the live check says the same BEFORE the button is pressed (the same refusal as the action)
    c = check(w, "A", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30")
    assert (c["ok"], c["code"], c["message"]) == (False, "REQUEST_REJECTED", "Администратор отклонил заявку по этому уроку")
    # the administrator moves it without any limit; the other lessons of the teacher are not touched by the rejection
    assert move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30").json()["result"]["kind"] == "reschedule"
    assert move(w, "A", w.G1, "lesson-l1", "2026-11-02", "2026-11-04", "12:00", "13:30").json()["result"]["kind"] == "reschedule"


def test_s4_a_rejection_of_another_teachers_request_does_not_block_this_one(w):
    """The rule is about HIS request: the lesson of A that a substitute D asked to give up (and was rejected) is still A's to move."""
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    item = one(w, "D", "L1", "2026-10-26")
    assert decide(w, "admin", item["id"], decision="reject", reason="Обратитесь к основному преподавателю").status_code == 200
    assert move(w, "D", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30", expect=403).json()["detail"]["code"] == "NOT_LESSON_TEACHER"       # a substitute never moves
    assert [r.status for r in requests_of(w) if r.requester_id == w.D.id] == ["rejected"]


# ===================================================================================================== S12: the substitute stays
def busy_d(w):
    """D teaches another group on Wednesday 28.10 at 12:00-13:30."""
    other = new_group(w.db, w.org, w.ids, "G5", "5-Д", w.D, [new_slot("lesson-l6", 3, "12:00", "13:30", "2026-10-12")], class_level=5)
    w.db.commit()
    return other


def assign_d(w):
    r = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    assert r.status_code == 200, r.text


def test_s12_the_time_of_the_substitute_is_checked_and_he_leads_the_new_lesson(w):
    busy_d(w)
    assign_d(w)
    # the main teacher A is free at 28.10 12:00, the substitute D is not: refused naming D, nothing moves; the live check tells the same
    r = move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30", expect=409)
    assert r.json()["detail"]["code"] == "TEACHER_BUSY"
    c = check(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30")
    assert c["ok"] is False and c["teacherId"] == str(w.D.id) and len(c["teacherConflicts"]) == 1
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None
    free = check(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "14:00", "15:30")
    assert free["ok"] is True and free["teacherId"] == str(w.D.id)
    out = move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "14:00", "15:30").json()["result"]
    new_id = out["newLesson"]["id"]
    assert (out["teacherId"], out["isSubstitution"]) == (str(w.D.id), True)
    w.db.expire_all()
    old = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-26").one()
    kept = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_id=new_id, lesson_date="2026-10-28", cancelled_at=None).one()
    assert old.cancelled_at is not None and kept.teacher_id == w.D.id
    assert client(w, "D").get(f"{SUB}/mine").json()["assignedToMe"][0]["lessonId"] == new_id
    lesson = next(o for o in client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-28", "date_to": "2026-10-28"}).json() if o["id"] == new_id)
    assert (lesson["teacherId"], lesson["isSubstitution"], lesson["movedFrom"]["date"]) == (str(w.D.id), True, "2026-10-26")


def test_s12_a_lesson_without_a_substitute_is_checked_for_the_main_teacher_as_before(w):
    busy_d(w)
    out = move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "12:00", "13:30").json()["result"]               # D is busy then, but D is nobody's substitute here
    assert out["teacherId"] is None and out["isSubstitution"] is None
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, cancelled_at=None).count() == 0


def test_s12_the_decision_of_a_request_keeps_the_substitute_too(w):
    """One road: the move through «Замены» of a lesson that has a substitute gives the same new lesson as the move from the lesson sheet."""
    busy_d(w)
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    other = one(w, "A", "L1", "2026-11-02")
    r = decide(w, "admin", other["id"], decision="substitute", teacherId=str(w.D.id))
    assert r.status_code == 200
    # the lesson with a substitute whose request was decided «substituted» is moved by the administrator from the sheet
    out = move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28", "14:00", "15:30").json()["result"]
    assert out["teacherId"] == str(w.D.id)


# ===================================================================================================== S12: STUDENT_BUSY
def s1_also_in_g4(w):
    w.db.add(StudentGroup(student_id=w.s1.id, group_id=w.G4.id, is_active=True, joined_at=datetime(2026, 9, 1, 5, 0)))
    w.db.commit()


def test_s12_a_student_with_another_lesson_at_the_new_time_is_a_warning_not_a_block(w):
    """Д30: s1 is in G1 and in G4 (Thursday 16:00-17:30); G1's Wednesday lesson moved to Thursday 16:30 -> one warning naming him, the move goes through."""
    s1_also_in_g4(w)
    c = check(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "16:30", "17:00")
    assert c["ok"] is True
    (warning,) = [x for x in c["warnings"] if x["code"] == "STUDENT_BUSY"]
    assert warning["students"] == [{"id": str(w.s1.id), "name": "Каримов А.", "groupName": "6-Г", "startTime": "16:00", "endTime": "17:30"}]
    assert "Каримов А. — «6-Г» 16:00–17:30" in warning["message"]
    r = move(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "16:30", "17:00")
    assert [x["code"] for x in r.json()["result"]["warnings"] if x["code"] == "STUDENT_BUSY"] == ["STUDENT_BUSY"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"                              # the move happened
    # the same warning from the teacher's own move
    s1_ok = check(w, "A", w.G1, "lesson-l1", "2026-11-02", "2026-11-05", "16:00", "17:00")
    assert [x["code"] for x in s1_ok["warnings"] if x["code"] == "STUDENT_BUSY"] == ["STUDENT_BUSY"]


def test_s12_no_warning_without_an_overlap_for_a_cancelled_lesson_or_a_student_who_left(w):
    s1_also_in_g4(w)
    assert [x for x in check(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "17:30", "18:00")["warnings"] if x["code"] == "STUDENT_BUSY"] == []         # touching is not overlapping
    client(w, "admin").post(f"{API}/org/groups/{w.G4.id}/attendance", json={"lessonDate": "2026-10-29", "lessonId": "lesson-l5", "status": "cancelled", "records": {},
                                                                           "cancellationReason": "Отмена"})
    assert [x for x in check(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "16:30", "17:00")["warnings"] if x["code"] == "STUDENT_BUSY"] == []     # the other lesson is cancelled
    w.db.query(StudentGroup).filter_by(student_id=w.s1.id, group_id=w.G4.id).update({"is_active": False})
    w.db.commit()
    assert [x for x in check(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "16:30", "17:00")["warnings"] if x["code"] == "STUDENT_BUSY"] == []


def test_s12_several_students_are_one_warning_with_the_names_cut(w):
    s1_also_in_g4(w)
    for st in (w.s2, w.s3):
        w.db.add(StudentGroup(student_id=st.id, group_id=w.G4.id, is_active=True, joined_at=datetime(2026, 9, 1, 5, 0)))
    w.db.commit()
    c = check(w, "admin", w.G1, "lesson-l2", "2026-10-28", "2026-10-29", "16:30", "17:00")
    (warning,) = [x for x in c["warnings"] if x["code"] == "STUDENT_BUSY"]
    assert [s["name"] for s in warning["students"]] == ["Каримов А.", "Орлова Д.", "Юсупов Б."] and warning["message"].count("«6-Г»") == 3
