"""LOGIC-B O5 / F6 - the teacher of a LESSON (substitution, handover, who taught).  Matrix rows [O5.R1] ... [O5.R9]."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from src.models.auth import GroupAttendance, GroupAudit, LessonTeacherAssignment

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg
LT = "/api/v1/auth/org/groups/{gid}/lesson-teacher"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d0(pg_session)


def put(w, who, group, teacher, *, lesson="lesson-l1", day="2026-10-26", reason="Болезнь", **extra):
    payload = {"lessonDate": day, "lessonId": lesson, "teacherId": str(getattr(w, teacher).id) if teacher else None, "reason": reason, **extra}
    return client(w, who).put(LT.format(gid=group.id), json=payload)


def active(w, group, lesson="lesson-l1", day="2026-10-26"):
    w.db.expire_all()
    return w.db.query(LessonTeacherAssignment).filter_by(group_id=group.id, lesson_id=lesson, lesson_date=day, cancelled_at=None).all()


def audits(w, group, action):
    w.db.expire_all()
    return w.db.query(GroupAudit).filter_by(group_id=group.id, action=action).all()


def occurrences_of(w, who, d_from="2026-10-26", d_to="2026-10-26", teacher=None):
    params = {"date_from": d_from, "date_to": d_to}
    if teacher:
        params["teacher_id"] = str(getattr(w, teacher).id)
    return client(w, who).get("/api/v1/auth/org/schedule/occurrences", params=params).json()


def test_r1_substitute_leads_the_lesson_and_is_paid_as_the_one_who_taught(w, clock):
    """[O5.R1] Mon 26.10 L1 of 9-А (teacher A) goes to D: it is D's lesson in the schedule, A's is gone; D starts it -> teacher_id = D, marked_by = D;
    an administrator who starts a substituted lesson himself leaves teacher_id = D, marked_by = him."""
    r = put(w, "admin", w.G1, "D")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["teacherId"] == str(w.D.id) and out["isSubstitution"] is True and out["defaultTeacherId"] == str(w.A.id)
    assert out["attendanceUpdated"] is False and out["version"] is None and out["payrollSheetsAffected"] == []
    mine = occurrences_of(w, "D", teacher="D")
    assert [(o["id"], o["isSubstitution"], o["teacherName"]) for o in mine] == [("lesson-l1", True, "Рахимов Т.")]
    assert [o["replacedBy"]["id"] for o in occurrences_of(w, "A", teacher="A") if o["id"] == "lesson-l1"] == [str(w.D.id)]       # W1 F10: the main teacher keeps his lesson, marked «Замена»
    dash = client(w, "D").get("/api/v1/auth/org/teacher/dashboard/summary", params={"business_date": "2026-10-26"}).json()
    assert [l["id"] for l in dash["todayLessons"]] == ["lesson-l1"] and dash["groups"] == []      # the lesson is his, the group is not ("Мои группы")
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-26").one()
    assert (row.teacher_id, row.marked_by) == (w.D.id, w.D.id)
    # the same on another lesson, started by the administrator
    put(w, "admin", w.G1, "D", lesson="lesson-l2", day="2026-10-28")
    clock.set("2026-10-28 16:05")
    assert client(w, "admin").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2"}).status_code == 200
    row2 = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-28").one()
    assert (row2.teacher_id, row2.marked_by) == (w.D.id, w.admin.id)


def test_r2_the_same_put_twice_and_the_group_teacher_means_no_substitution(w):
    """[O5.R2]"""
    put(w, "admin", w.G1, "D")
    before = len(audits(w, w.G1, "lesson_teacher_set"))
    again = put(w, "admin", w.G1, "D")
    assert again.status_code == 200 and again.json()["teacherId"] == str(w.D.id)
    assert len(active(w, w.G1)) == 1 and len(audits(w, w.G1, "lesson_teacher_set")) == before          # nothing new
    back = put(w, "admin", w.G1, "A")                                                                  # = the group teacher: a clear
    assert back.status_code == 200 and back.json()["isSubstitution"] is False and active(w, w.G1) == []


def test_r3_taking_the_substitution_off(w):
    """[O5.R3] PUT teacherId null: the assignment is cancelled (kept), a row of the lesson gets the default teacher back, audit lesson_teacher_cleared."""
    put(w, "admin", w.G1, "D")
    out = put(w, "admin", w.G1, None, reason="Вернулся")
    assert out.status_code == 200 and out.json()["teacherId"] == str(w.A.id) and out.json()["isSubstitution"] is False
    w.db.expire_all()
    rows = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id).all()
    assert len(rows) == 1 and rows[0].cancelled_at is not None and rows[0].cancelled_by == w.admin.id
    assert len(audits(w, w.G1, "lesson_teacher_cleared")) == 1


def test_r4_two_admins_and_the_expected_teacher(w):
    """[O5.R4] Admin 1 assigns D; admin 2 (who saw the lesson with A) sends expectedTeacherId=A -> 409 LESSON_TEACHER_CHANGED with the current one; without it the last one wins and
    both are in the audit."""
    assert put(w, "admin", w.G1, "D").status_code == 200
    stale = put(w, "owner", w.G1, "B", expectedTeacherId=str(w.A.id))
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "LESSON_TEACHER_CHANGED"
    assert stale.json()["detail"]["currentTeacherId"] == str(w.D.id)
    last = put(w, "owner", w.G1, "C")
    assert last.status_code == 200 and active(w, w.G1)[0].teacher_id == w.C.id
    assert len(audits(w, w.G1, "lesson_teacher_set")) == 2


def test_r4b_parallel_assignments_leave_exactly_one_active_row(w):
    """[O5.R4] Two administrators assign D and C at the same moment: the group lock + the partial unique index keep one active row."""
    from src.auth.lesson_teachers import LessonTeachers
    from src.auth.ops_schemas import LessonTeacherPut

    def call(teacher):
        def go(svc):
            req = LessonTeacherPut(lessonDate=date(2026, 10, 26), lessonId="lesson-l1", teacherId=str(getattr(w, teacher).id), reason="Болезнь")
            return LessonTeachers(svc).put(str(w.org.id), w.G1.id, req, actor_id=str(w.admin.id))
        return go

    results = run_parallel(w, call("D"), call("C"))
    assert all(err is None for _r, err in results), results
    assert len(active(w, w.G1)) == 1


def test_r4c_a_substitution_during_a_start_is_refused_and_the_teacher_keeps_the_lesson(w, clock):
    """[O5.R4 + W1 S7 / D20] C started the lesson a moment before the substitution: a lesson that is going on keeps its teacher (409 ``LESSON_IN_PROGRESS``), C's marks go on."""
    w.G1.teacher_id = w.C.id
    w.db.commit()
    clock.set("2026-10-26 10:05")
    cl = client(w, "C")
    base = f"/api/v1/auth/org/groups/{w.G1.id}/attendance"
    assert cl.post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    assert cl.post(base, json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress",
                               "records": {str(w.s1.id): "present"}, "expectedVersion": 1}).status_code == 200
    refused = put(w, "admin", w.G1, "D")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "LESSON_IN_PROGRESS"
    ok = cl.post(base, json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress",
                             "records": {str(w.s1.id): "present", str(w.s2.id): "absent"}})
    assert ok.status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-26").one()
    assert row.teacher_id == w.C.id and row.records == {str(w.s1.id): "present", str(w.s2.id): "absent"}


def test_r5_who_taught_a_finished_lesson_can_be_corrected_inside_the_window_only(w, monkeypatch, clock):
    """[O5.R5] Admin / owner, window from the 1st of the previous month (a saved / paid sheet never blocks the change: it becomes «data changed»); older than the window -> 422;
    a missed lesson is reconciled by the teacher OF THAT DATE, not by the new teacher."""
    r = put(w, "admin", w.G1, "D", lesson="lesson-l1", day="2026-10-19", reason="Исправление")
    assert r.status_code == 200 and r.json()["attendanceUpdated"] is True and r.json()["version"] == 2
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-19").one()
    assert row.teacher_id == w.D.id and row.marked_by == w.A.id                                # marked_by is not "who taught"
    kinds = [a.action for a in w.db.execute(text("SELECT action FROM group_attendance_audit WHERE attendance_id=:i"), {"i": row.id}).fetchall()] if False else \
        [r_[0] for r_ in w.db.execute(text("SELECT action FROM group_attendance_audit WHERE attendance_id=:i"), {"i": row.id}).fetchall()]
    assert "teacher_changed" in kinds

    old = put(w, "admin", w.G1, "B", lesson="lesson-l1", day="2026-08-31", reason="Исправление")
    assert old.status_code == 422 and old.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"


def test_r6_group_teacher_change_by_the_end_of_the_lesson(w, clock):
    """[O5.R6] A change on Wed at 14:00: lessons that ended by 14:00 stay with the old teacher, later ones go to the new; the border of midnight is the same rule."""
    from src.auth.lesson_teachers import lesson_teacher

    cl = client(w, "admin")
    assert cl.put(f"/api/v1/auth/org/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)}).status_code == 200
    w.db.expire_all()
    g1 = w.db.get(type(w.G1), w.G1.id)
    assert lesson_teacher(w.db, g1, "lesson-l2", "2026-10-21", {"endTime": "17:30"}) == w.C.id      # ends 12:30 UTC > the change at 09:00 UTC
    assert lesson_teacher(w.db, g1, "lesson-l1", "2026-10-19", {"endTime": "11:30"}) == w.A.id      # ended before the change
    assert lesson_teacher(w.db, g1, "lesson-l1", "2026-10-26", {"endTime": "11:30"}) == w.C.id
    # a change on Tue 22:50: the lesson Wed 00:30-01:30 goes to the new teacher, Tue 10:00-11:00 (never started) stays with the old one
    g2 = w.G2
    clock.set("2026-10-27 22:50")
    assert cl.put(f"/api/v1/auth/org/groups/{g2.id}", json={"teacher_id": str(w.B.id)}).status_code == 200
    w.db.expire_all()
    g2 = w.db.get(type(w.G2), w.G2.id)
    assert lesson_teacher(w.db, g2, "lesson-l3", "2026-10-27", {"endTime": "11:00"}) == w.A.id
    assert lesson_teacher(w.db, g2, "lesson-l3", "2026-10-28", {"endTime": "01:30"}) == w.B.id


def test_r6b_substitution_on_the_month_border_follows_the_lesson_date(w, clock):
    """[O5.R6] A substitution on 31.10 and one on 01.11 belong to their own months by ``lesson_date`` (the row of 31.10 completed after midnight stays October's)."""
    w.G3.schedule = [new_slot("lesson-l4", 6, "23:00", "23:59", "2026-10-19"), new_slot("lesson-l5", 7, "00:00", "01:00", "2026-10-19")]
    w.db.commit()
    assert put(w, "admin", w.G3, "D", lesson="lesson-l4", day="2026-10-31").status_code == 200
    assert put(w, "admin", w.G3, "D", lesson="lesson-l5", day="2026-11-01").status_code == 200
    days = sorted(a.lesson_date for a in w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G3.id, teacher_id=w.D.id))
    assert days == ["2026-10-31", "2026-11-01"] and [d[:7] for d in days] == ["2026-10", "2026-11"]


def test_r7_no_teacher_candidates_busy_and_cancelled(w):
    """[O5.R7] A group without a teacher takes a substitution; the target must be a teaching member (422), free (409 TEACHER_BUSY with conflicts) and the lesson not cancelled (409)."""
    w.G1.teacher_id = None
    w.db.commit()
    assert put(w, "admin", w.G1, "D").status_code == 200
    bad = client(w, "admin").put(LT.format(gid=w.G1.id), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.foreign_admin.id), "reason": "Болезнь"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "TARGET_NOT_TEACHER"
    busy = put(w, "admin", w.G1, "B")                                       # B teaches 8-В on Mondays 10:00-11:30
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "TEACHER_BUSY"
    first = busy.json()["detail"]["conflicts"][0]
    assert first == {"date": "2026-10-26", "startTime": "10:00", "endTime": "11:30", "groupId": str(w.G1.id), "groupName": "9-А", "withGroupName": "8-В"}
    history_row(w.db, w.org, w.G1, "lesson-l1", "2026-11-02", None, [w.s1], status="cancelled", records={})
    w.db.commit()
    gone = put(w, "admin", w.G1, "D", day="2026-11-02")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "LESSON_CANCELLED"


def test_r7b_a_lesson_without_a_teacher_finished_by_an_admin_has_no_teacher(w, clock):
    """[O5.R7] A group without a teacher: the lesson an admin completes has teacher_id NULL (payroll counts it as "without a teacher")."""
    w.G1.teacher_id = None
    w.db.commit()
    clock.set("2026-10-21 16:30")
    cl = client(w, "admin")
    base = f"/api/v1/auth/org/groups/{w.G1.id}/attendance"
    assert cl.post(base + "/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()
    assert row.teacher_id is None and row.marked_by == w.admin.id
    clock.set("2026-10-21 17:20")
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    assert cl.post(base, json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "records": marks, "expectedVersion": 1}).status_code == 200
    w.db.refresh(row)
    assert row.teacher_id is None
    assert put(w, "admin", w.G1, "D", lesson="lesson-l2", day="2026-10-21", reason="Назначить вручную").json()["attendanceUpdated"] is True


def test_r8_only_managers_assign_and_the_substitute_acts_on_his_lesson_only(w, clock):
    """[O5.R8] A teacher cannot assign (403) - neither himself nor others; the substitute reads the group and works only his lesson; the replaced teacher gets 403 NOT_LESSON_TEACHER;
    a substitute does not see student profiles."""
    assert put(w, "A", w.G1, "A").status_code == 403
    assert put(w, "D", w.G1, "D").status_code == 403
    assert put(w, "admin", w.G1, "D").status_code == 200
    d = client(w, "D")
    assert d.get(f"/api/v1/auth/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).status_code == 200      # reads the journal
    assert d.get(f"/api/v1/auth/org/groups/{w.G1.id}").status_code == 403                                              # but not the group page (R3-5)
    assert d.get(f"/api/v1/auth/org/students/{w.s1.id}/profile").status_code in (403, 404)                            # not the profiles
    base = f"/api/v1/auth/org/groups/{w.G1.id}/attendance"
    clock.set("2026-10-28 16:05")
    not_his = d.post(base + "/start", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2"})            # Wednesday's lesson is A's
    assert not_his.status_code == 403 and not_his.json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    assert not_his.json()["detail"]["message"] == "Этот урок ведёт Иванов И."
    clock.set("2026-10-26 10:05")
    other = client(w, "A").post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})     # the substituted lesson is not A's
    assert other.status_code == 403 and other.json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    assert d.post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200  # the substitute works his own lesson


def test_r8b_substitute_window_is_seven_days(w, clock):
    """[O5.R8] The substitute reads the group while he has an assignment with lesson_date >= today - 7."""
    assert put(w, "admin", w.G1, "D", day="2026-10-26").status_code == 200
    clock.set("2026-11-02")      # the lesson is 7 days old: still readable
    assert client(w, "D").get(f"/api/v1/auth/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).status_code == 200
    clock.set("2026-11-03")      # 8 days
    assert client(w, "D").get(f"/api/v1/auth/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).status_code == 403


def test_r9_import_group_without_history_follows_the_current_teacher_and_old_rows_keep_theirs(w):
    """[O5.R9] A group with no teacher history (created by an import, audit ``created`` only): the teacher is the current one; rows written before keep their own teacher_id."""
    from src.auth.lesson_teachers import group_teacher_at

    group = new_group(w.db, w.org, w.ids, "Gi", "Импорт", w.C, [new_slot("lesson-i", 1, "10:00", "11:00", "2026-10-12")])
    old_row = history_row(w.db, w.org, group, "lesson-i", "2026-10-12", None, [], status="completed", records={})
    w.db.commit()
    assert group_teacher_at(w.db, group, datetime(2026, 10, 1)) == w.C.id
    assert old_row.teacher_id is None


def test_handover_semantics_in_progress_lesson_stays_with_the_old_teacher(w, clock):
    """[O5.R6] A running lesson stays with the teacher who started it (his row); the new group teacher does not touch it (INV-O5-5: the row is not rewritten)."""
    clock.set("2026-10-21 16:20")
    base = f"/api/v1/auth/org/groups/{w.G1.id}/attendance"
    assert client(w, "A").post(base + "/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    assert client(w, "admin").put(f"/api/v1/auth/org/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)}).status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()
    assert row.teacher_id == w.A.id
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    assert client(w, "C").post(base, json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "in_progress", "records": marks}).status_code == 403
    assert client(w, "A").post(base, json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "in_progress", "records": marks}).status_code == 200


def test_group_teacher_change_checks_the_overlaps_of_the_new_teacher(w):
    """[O5.R6] PUT teacher_id: B already has Mondays 10:00-11:30 (8-В): moving 9-А to B is 409 TEACHER_BUSY (conflicts up to 20, total); an equal substitution is taken off."""
    r = client(w, "admin").put(f"/api/v1/auth/org/groups/{w.G1.id}", json={"teacher_id": str(w.B.id)})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_BUSY"
    assert r.json()["detail"]["total"] == 8 and r.json()["detail"]["conflicts"][0]["date"] == "2026-10-26"
    assert put(w, "admin", w.G1, "C", day="2026-10-26").status_code == 200
    assert client(w, "admin").put(f"/api/v1/auth/org/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)}).status_code == 200
    assert active(w, w.G1) == [] and len(audits(w, w.G1, "lesson_teacher_cleared")) == 1


def test_inv_o5_candidates_busy_flags(w):
    """[INV-O5-3] The candidates list marks the teachers who already have a lesson then (and says with whom)."""
    r = client(w, "admin").get(LT.format(gid=w.G1.id).replace("lesson-teacher", "lesson-teacher-candidates"),
                               params={"lessonId": "lesson-l1", "lessonDate": "2026-10-26"})
    assert r.status_code == 200
    by_name = {c["name"]: c for c in r.json()["candidates"]}
    assert by_name["Петров П."]["busy"] is True and by_name["Петров П."]["busyWith"] == {"groupName": "8-В", "startTime": "10:00", "endTime": "11:30"}
    assert by_name["Рахимов Т."]["busy"] is False and "Усманов У." not in by_name         # the owner leads no lesson (W1 round 2)
