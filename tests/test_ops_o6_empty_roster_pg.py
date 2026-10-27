"""LOGIC-B O6 - a lesson with nobody in its roster is neither started nor completed.  Matrix rows [O6.R1] ... [O6.R9]."""
from datetime import date, datetime, timedelta

import pytest

from src.auth.schemas import ReconcileMissedLessonRequest, StartAttendanceRequest
from src.models.auth import GroupAttendance, StudentGroup

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 16:05")          # the lesson L2 of G1 (Wed 16:00-17:30) is running
    return build_d0(pg_session)


def start(w, who, group, lesson, day="2026-10-21"):
    return client(w, who).post(f"/api/v1/auth/org/groups/{group.id}/attendance/start", json={"lessonDate": day, "lessonId": lesson})


def empty_group(w, name="Пустая", teacher=None):
    teacher = teacher or w.C
    group = new_group(w.db, w.org, w.ids, "E" + name, name, teacher, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-14")])
    w.db.commit()
    return group


def test_r1_group_with_students_starts_as_before(w):
    """[O6.R1]"""
    r = start(w, "A", w.G1, "lesson-l2")
    assert r.status_code == 200 and r.json() == {"ok": True, "started": True, "version": 1}


def test_r2_empty_group_never_starts_every_time_409(w):
    """[O6.R2]"""
    group = empty_group(w)
    for _ in range(2):
        r = start(w, "C", group, "lesson-e")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "NO_STUDENTS"
        assert r.json()["detail"]["message"] == "В группе нет учеников — урок начать нельзя. Добавьте учеников в группу."
    assert w.db.query(GroupAttendance).filter_by(group_id=group.id).count() == 0


def test_r3_legacy_open_lesson_with_empty_roster_cannot_be_completed_but_can_be_cancelled(w, clock):
    """[O6.R3]"""
    group = empty_group(w, "Легаси")
    row = history_row(w.db, w.org, group, "lesson-e", "2026-10-21", w.C, [], status="in_progress", start="16:00")
    w.db.commit()
    clock.set("2026-10-21 17:20")                                  # inside the last 15 minutes: the lesson may be completed
    c = client(w, "C")
    done = c.post(f"/api/v1/auth/org/groups/{group.id}/attendance",
                  json={"lessonDate": "2026-10-21", "lessonId": "lesson-e", "status": "completed", "records": {}})
    assert done.status_code == 409 and done.json()["detail"]["code"] == "NO_STUDENTS" and done.json()["detail"]["alias"] == "NO_STUDENTS_COMPLETE"
    assert "Отметьте «Урока не было»" in done.json()["detail"]["message"]
    cancel = c.post(f"/api/v1/auth/org/groups/{group.id}/attendance",
                    json={"lessonDate": "2026-10-21", "lessonId": "lesson-e", "status": "cancelled", "records": {},
                          "cancellationReason": "Урока не было"})
    assert cancel.status_code == 200, cancel.text
    w.db.refresh(row)
    assert row.status == "cancelled"


def test_r4_removing_the_last_student_and_starting_are_serialised(w):
    """[O6.R4] An administrator removes the last student while the teacher starts: either the start wins (the removal is refused while a
    lesson runs) or the removal wins (the start is NO_STUDENTS) - never a running lesson with an empty roster."""
    group = new_group(w.db, w.org, w.ids, "Gone", "Одинокая", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-14")])
    only = new_student(w.db, w.org, w.ids, "only", "Одинов", "Олим", group)
    w.db.commit()
    org, gid, sid = str(w.org.id), str(group.id), str(only.id)
    results = run_parallel(
        w,
        lambda svc: svc.start_group_attendance(org, gid, StartAttendanceRequest(lessonDate=date(2026, 10, 21), lessonId="lesson-e"),
                                               started_by=str(w.C.id), is_org_admin=False),
        lambda svc: svc.remove_student_from_group(org, gid, sid),
    )
    started_ok, removed_ok = results[0][1] is None, results[1][1] is None
    w.db.expire_all()
    rows = w.db.query(GroupAttendance).filter_by(group_id=group.id).all()
    members = w.db.query(StudentGroup).filter_by(group_id=group.id, is_active=True).count()
    assert started_ok != removed_ok or (not started_ok and not removed_ok), results
    if started_ok:
        assert rows and members == 1                       # the removal was refused: "пока идёт урок"
        assert "пока идёт урок" in str(results[1][1])
    else:
        assert not rows and members == 0
        assert code_of(results[0][1]) == "NO_STUDENTS"


def test_r5_reconcile_refuses_an_empty_roster_cancel_missed_does_not(w):
    """[O6.R5] reconcile of a past lesson with nobody enrolled at that moment is refused (as before); cancelling it ("Урока не было") works."""
    group = empty_group(w, "Пропущенная")
    day = date(2026, 10, 14)
    req = StartAttendanceRequest(lessonDate=day, lessonId="lesson-e")
    with pytest.raises(ValueError, match="не было учеников"):
        w.svc.reconcile_missed_group_attendance(str(w.org.id), str(group.id), req, reconciled_by=str(w.admin.id), is_org_admin=True)
    assert w.svc.cancel_missed_group_attendance(
        str(w.org.id), str(group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId="lesson-e", cancellationReason="Урока не было"),
        cancelled_by=str(w.admin.id), is_org_admin=True)["version"] == 1


def test_r6_roster_boundaries_live_start_by_the_active_members_reconcile_by_the_periods(w, clock):
    """[O6.R6] A student who joined at 10:05 for the 10:00 lesson: the LIVE start (10:06) counts him (he is active); a reconcile by the
    planned start does not; joined_at == the planned start counts, left_at == the planned start does not."""
    group = new_group(w.db, w.org, w.ids, "Gb", "Граница", w.C, [new_slot("lesson-b", 3, "10:00", "11:00", "2026-10-14")])
    late = new_student(w.db, w.org, w.ids, "late", "Поздний", "Ученик", group, joined=datetime(2026, 10, 21, 5, 5))      # 10:05 Tashkent
    exact = new_student(w.db, w.org, w.ids, "exact", "Точный", "Ученик", group, joined=datetime(2026, 10, 21, 5, 0))      # == 10:00 Tashkent
    gone = new_student(w.db, w.org, w.ids, "gone", "Ушедший", "Ученик", group, joined=datetime(2026, 9, 1, 5, 0))
    membership = w.db.query(StudentGroup).filter_by(student_id=gone.id).one()
    from src.auth.membership_periods import close_period

    close_period(membership, datetime(2026, 10, 21, 5, 0))                                                                # left == 10:00
    w.db.commit()
    clock.set("2026-10-21 10:06")
    assert start(w, "C", group, "lesson-b").status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=group.id).one()
    assert {r["student_id"] for r in row.roster_snapshot} == {str(late.id), str(exact.id)}
    clock.set("2026-10-22 12:00")
    roster = w.svc._attendance_roster_at(group.id, datetime(2026, 10, 21, 5, 0))
    assert {r["student_id"] for r in roster} == {str(exact.id)}                       # joined == instant: in; left == instant: out; later: out


def test_r7_all_students_archived_means_empty(w):
    """[O6.R7]"""
    group = new_group(w.db, w.org, w.ids, "Garch", "Архивные", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-14")])
    new_student(w.db, w.org, w.ids, "arch1", "Архивов", "Ушёл", group, status="archived")
    w.db.commit()
    r = start(w, "C", group, "lesson-e")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NO_STUDENTS"


def test_r8_teacher_and_admin_are_treated_alike(w):
    """[O6.R8]"""
    group = empty_group(w, "Роли")
    assert start(w, "C", group, "lesson-e").json()["detail"]["code"] == "NO_STUDENTS"
    assert start(w, "admin", group, "lesson-e").json()["detail"]["code"] == "NO_STUDENTS"


def test_r9_imported_group_without_students_cannot_start(w):
    """[O6.R9]"""
    group = new_group(w.db, w.org, w.ids, "Gimp", "Импорт", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-21")])
    w.db.commit()
    assert start(w, "admin", group, "lesson-e").status_code == 409


def test_inv_o6_1_completed_lessons_have_a_roster(w):
    """[INV-O6-1] A lesson started through the service always has a non-empty roster snapshot."""
    assert start(w, "A", w.G1, "lesson-l2").status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()
    assert len(row.roster_snapshot) == 3
