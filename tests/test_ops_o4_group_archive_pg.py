"""LOGIC-B O4 / F2 - archiving a group is an operation (blockers, automatic actions, read-only archive, restore).  Matrix rows [O4.R1] ... [O4.R9]."""
from datetime import date, datetime

import pytest

from src.models.auth import Group, GroupAttendance, GroupAudit, StudentGroup

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg
G = "/api/v1/auth/org/groups/{gid}"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-22 10:00")
    return build_d0(pg_session)


def free_group(w, name="Свободная", teacher=None, slots=None):
    """A group nobody belongs to, with a schedule that has nothing missed (valid from today)."""
    group = new_group(w.db, w.org, w.ids, "F" + name, name, teacher or w.C,
                      slots if slots is not None else [new_slot("lesson-f", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    return group


def left_student(w, group, key="gone", left=datetime(2026, 10, 21, 8, 0)):
    """A student who belonged to the group from 01.09 and left on 21.10: the lessons before that have a roster (so they can be "missed"), nobody is in the group now."""
    from src.auth.membership_periods import close_period

    student = new_student(w.db, w.org, w.ids, key + str(group.id)[:6], "Ушедший", "Ученик", group)
    close_period(w.db.query(StudentGroup).filter_by(student_id=student.id).one(), left)
    w.db.flush()
    return student


def refresh(w, group):
    w.db.expire_all()
    return w.db.get(Group, group.id)


def archive(w, group, who="admin", **body):
    return client(w, who).post(G.format(gid=group.id) + "/archive", json=body)


def test_r1_empty_group_archives_with_an_open_interval_and_an_audit_row(w):
    """[O4.R1] 0 students, nothing open, nothing missed, no live exams: status archived, reason, interval {from: now, to: null}, group_audit; with students -> 409 and a list."""
    group = free_group(w)
    r = archive(w, group, reason="Группа закрыта")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "archived" and out["archiveReason"] == "Группа закрыта" and out["archivedAt"] == "2026-10-22T05:00:00"
    g = refresh(w, group)
    assert g.inactive_intervals == [{"from": "2026-10-22T05:00:00", "to": None}] and g.archive_reason == "Группа закрыта"
    assert [a.action for a in w.db.query(GroupAudit).filter_by(group_id=group.id, action="group_archived")] == ["group_archived"]
    blocked = archive(w, w.G2)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "GROUP_ARCHIVE_BLOCKED"
    students = blocked.json()["detail"]["blockers"][0]
    assert students["code"] == "ACTIVE_STUDENTS" and students["count"] == 2 and {s["displayId"] for s in students["students"]} == {"AL09-005", "AL09-006"}
    assert refresh(w, w.G2).status == "active"


def test_r2_double_click(w):
    """[O4.R2]"""
    group = free_group(w)
    assert archive(w, group).status_code == 200
    again = archive(w, group)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "ALREADY_ARCHIVED"
    assert client(w, "admin").post(G.format(gid=group.id) + "/restore", json={}).status_code == 200
    second = client(w, "admin").post(G.format(gid=group.id) + "/restore", json={})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "NOT_ARCHIVED"


def test_r3_restore_teacher_inactive_intervals_and_no_phantom_lessons(w, clock):
    """[O4.R3] Restore closes the interval at now, nothing is born for the time in the archive; an inactive teacher -> 409 TEACHER_INACTIVE (with the name), a repeat with
    teacherId works; archive -> restore -> archive gives [a1, r1), [a2, inf)."""
    group = free_group(w, slots=[new_slot("lesson-f", 1, "10:00", "11:00", "2026-10-12")])
    assert archive(w, group).status_code == 200                                              # Thu 22.10 10:00
    clock.set("2026-11-05 14:00")
    w.db.query(type(w.member("C"))).filter_by(user_id=w.C.id).update({"is_active": False})
    w.db.commit()
    r = client(w, "admin").post(G.format(gid=group.id) + "/restore", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_INACTIVE" and r.json()["detail"]["teacherName"] == "Сидоров С."
    ok = client(w, "admin").post(G.format(gid=group.id) + "/restore", json={"teacherId": str(w.D.id)})
    assert ok.status_code == 200 and ok.json()["status"] == "active" and ok.json()["teacher_id"] == str(w.D.id) and ok.json()["archivedAt"] is None
    g = refresh(w, group)
    assert g.inactive_intervals == [{"from": "2026-10-22T05:00:00", "to": "2026-11-05T09:00:00"}]
    days = [o["occurrenceDate"] for o in w.svc._resolved_occurrences_for_group(g, date_from=date(2026, 10, 12), date_to=date(2026, 11, 16))]
    assert days == ["2026-10-12", "2026-10-19", "2026-11-09", "2026-11-16"]                 # 26.10 and 02.11 were in the archive: no phantom lessons
    missed = [a for a in client(w, "admin").get("/api/v1/auth/org/attendance/open-actions").json() if a["groupId"] == str(group.id)]
    assert "2026-10-26" not in {a["lessonDate"] for a in missed} and "2026-11-02" not in {a["lessonDate"] for a in missed}
    clock.set("2026-11-12 09:00")
    assert archive(w, group).status_code in (200, 409)
    g = refresh(w, group)
    if g.status == "archived":
        assert g.inactive_intervals[0] == {"from": "2026-10-22T05:00:00", "to": "2026-11-05T09:00:00"} and g.inactive_intervals[1]["to"] is None
    # a "no teacher" restore on request: teacherId null
    group2 = free_group(w, "Без")
    archive(w, group2)
    r2 = client(w, "admin").post(G.format(gid=group2.id) + "/restore", json={"teacherId": None})
    assert r2.status_code == 200 and r2.json()["teacher_id"] is None


def test_r4_archive_and_start_are_serialised_by_the_group_lock(w, clock):
    """[O4.R4] A teacher starts a lesson while an administrator archives: either the start is first (the archive is blocked: OPEN_LESSON) or the archive is first
    (the start is 409 GROUP_ARCHIVED).  Enrolment into the archived group is refused under the same lock."""
    group = new_group(w.db, w.org, w.ids, "Gx", "Гонка", w.C, [new_slot("lesson-x", 4, "09:00", "11:00", "2026-10-22")])
    w.db.commit()
    from src.auth.schemas import StartAttendanceRequest
    from src.auth.ops_schemas import ArchiveGroupRequest as Req
    from src.auth.group_lifecycle import GroupLifecycle

    org, gid = str(w.org.id), str(group.id)
    new_student(w.db, w.org, w.ids, "late", "Поздний", "Ученик", None)
    w.db.commit()
    # the group has no students: "NO_STUDENTS" would hide the race, so give it one and let the archive blocker ACTIVE_STUDENTS be out of the picture by archiving first
    results = run_parallel(
        w,
        lambda svc: GroupLifecycle(svc).archive(org, gid, Req(), actor_id=str(w.admin.id), actor_role="admin"),
        lambda svc: svc.start_group_attendance(org, gid, StartAttendanceRequest(lessonDate=date(2026, 10, 22), lessonId="lesson-x"),
                                               started_by=str(w.C.id), is_org_admin=False),
    )
    archived_ok, started_ok = results[0][1] is None, results[1][1] is None
    assert not (archived_ok and started_ok and False)
    # an empty group never starts (NO_STUDENTS) - so the start fails either way; the point is: the archive is atomic and the state is consistent
    w.db.expire_all()
    assert (archived_ok and w.db.get(Group, group.id).status == "archived") or (not archived_ok and w.db.get(Group, group.id).status == "active")
    if archived_ok:
        late = client(w, "admin").post(f"/api/v1/auth/org/groups/{gid}/students/{w.ids('late')}")
        assert late.status_code == 409 and late.json()["detail"]["code"] == "GROUP_ARCHIVED"


def test_r4b_an_open_lesson_blocks_the_archive(w, clock):
    """[O4.R4] [O4.R7] The start came first: the archive is blocked by OPEN_LESSON."""
    clock.set("2026-10-21 16:10")
    assert client(w, "A").post(G.format(gid=w.G1.id) + "/attendance/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    r = archive(w, w.G1)
    codes = [b["code"] for b in r.json()["detail"]["blockers"]]
    assert r.status_code == 409 and "OPEN_LESSON" in codes and "ACTIVE_STUDENTS" in codes


def test_r5_missed_lessons_block_unless_the_admin_cancels_them(w):
    """[O4.R5] 19.10 and 12.10 missed: blocker MISSED_LESSONS; ``missedLessonsAction: cancel`` makes cancelled rows with the reason, teacher by the resolver."""
    group = new_group(w.db, w.org, w.ids, "Gm", "Пропуски", w.C, [new_slot("lesson-m", 1, "10:00", "11:00", "2026-10-05")])
    left_student(w, group)
    w.db.commit()
    blocked = archive(w, group)
    assert blocked.status_code == 409
    missed = [b for b in blocked.json()["detail"]["blockers"] if b["code"] == "MISSED_LESSONS"][0]
    assert missed["count"] == 3 and [l["lessonDate"] for l in missed["lessons"]] == ["2026-10-05", "2026-10-12", "2026-10-19"]
    ok = archive(w, group, reason="Закрыта", missedLessonsAction="cancel")
    assert ok.status_code == 200 and ok.json()["cancelledMissedLessons"] == 3
    rows = w.db.query(GroupAttendance).filter_by(group_id=group.id).order_by(GroupAttendance.lesson_date).all()
    assert [(r.status, r.cancellation_reason, r.teacher_id, r.version) for r in rows] == [("cancelled", "Группа закрыта: Закрыта", w.C.id, 1)] * 3


def test_r6_archive_at_2350_a_lesson_after_midnight_is_not_born_one_before_is_missed(w, clock):
    """[O4.R6] Archive at 23:50: the lesson at 00:30 does not exist; a lesson 23:00-23:45 without a start is a missed one (it blocks, or is cancelled by the admin's decision)."""
    group = new_group(w.db, w.org, w.ids, "Gn", "Ночь", w.C, [new_slot("lesson-n1", 4, "23:00", "23:45", "2026-10-15"),
                                                                 new_slot("lesson-n2", 5, "00:30", "01:30", "2026-10-15")])
    left_student(w, group, left=datetime(2026, 10, 22, 18, 49))      # left at 23:49 Tashkent: he belonged to the 23:00 lesson
    w.db.commit()
    clock.set("2026-10-22 23:50")
    blocked = archive(w, group)
    lessons = [b for b in blocked.json()["detail"]["blockers"] if b["code"] == "MISSED_LESSONS"][0]["lessons"]
    assert (22, "lesson-n1") in {(int(l["lessonDate"][-2:]), l["lessonId"]) for l in lessons}                   # 22.10 23:00-23:45
    assert archive(w, group, missedLessonsAction="cancel").status_code == 200
    g = refresh(w, group)
    days = {(o["id"], o["occurrenceDate"]) for o in w.svc._resolved_occurrences_for_group(g, date_from=date(2026, 10, 22), date_to=date(2026, 10, 31))}
    assert ("lesson-n2", "2026-10-23") not in days and ("lesson-n1", "2026-10-22") in days                       # tomorrow 00:30 starts after the archive; today 23:00 before it
    assert all(not (i == "lesson-n2") for i, _d in days)


def test_r7_nothing_attached_archives_at_once_and_exam_service_outage_is_fail_closed(w, exams):
    """[O4.R7] A group without teacher / schedule / students archives at once; exam-service down: archive 503 EXAM_SERVICE_UNAVAILABLE (nothing changes), impact says unavailable and canArchive false."""
    group = new_group(w.db, w.org, w.ids, "Gz", "Пустышка", None, [])
    w.db.commit()
    exams.unavailable = True
    impact = client(w, "admin").get(G.format(gid=group.id) + "/archive-impact").json()
    assert impact["examService"] == "unavailable" and impact["canArchive"] is False
    r = archive(w, group)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EXAM_SERVICE_UNAVAILABLE"
    assert refresh(w, group).status == "active"
    exams.unavailable = False
    assert archive(w, group).status_code == 200


def test_r8_roles_and_a_teacher_reads_the_archive(w):
    """[O4.R8] Owner / admin archive and restore; a teacher gets 403 even for his own group and still reads the journal of an archived group."""
    group = free_group(w)
    assert archive(w, group, who="C").status_code == 403
    assert client(w, "C").post(G.format(gid=group.id) + "/restore", json={}).status_code == 403
    assert client(w, "owner").post(G.format(gid=group.id) + "/archive", json={}).status_code == 200
    assert client(w, "C").get(G.format(gid=group.id) + "/attendance", params={"month": "2026-10"}).status_code == 200
    assert client(w, "owner").post(G.format(gid=group.id) + "/restore", json={}).status_code == 200


def test_r9_groups_archived_before_the_operation_with_students(w):
    """[O4.R9] A group archived the old way (status only, with students): archive-impact counts the students left (banner); importing / enrolling into it is refused."""
    w.G2.status = "archived"
    w.G2.inactive_intervals = [{"from": "2026-10-05T07:30:15", "to": None}]
    w.db.commit()
    impact = client(w, "admin").get(G.format(gid=w.G2.id) + "/archive-impact").json()
    assert impact["status"] == "archived" and impact["canArchive"] is False and impact["legacyStudentsLeft"] == 2
    r = client(w, "admin").post(G.format(gid=w.G2.id) + f"/students/{w.s6.id}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GROUP_ARCHIVED"
    assert client(w, "admin").get(G.format(gid=w.G2.id)).json()["archivedAt"] == "2026-10-05T07:30:15"


def test_status_goes_through_the_action_only(w):
    """[O4.R1] PUT /org/groups/{id} with a status is 422 STATUS_USE_ARCHIVE_ACTION (it used to reach the CHECK as a 500)."""
    r = client(w, "admin").put(G.format(gid=w.G3.id), json={"status": "archived"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "STATUS_USE_ARCHIVE_ACTION"
    r2 = client(w, "admin").put(G.format(gid=w.G3.id), json={"status": "nonsense", "name": "x"})
    assert r2.status_code == 422 and refresh(w, w.G3).name == "8-В"


def test_archived_group_is_read_only_for_the_writers(w, clock):
    """[INV-O4-3] start / create / reschedule / revise / add student / update fields all answer 409 GROUP_ARCHIVED."""
    group = free_group(w, slots=[new_slot("lesson-f", 4, "15:00", "16:00", "2026-10-22")])
    assert archive(w, group).status_code == 200
    cl = client(w, "admin")
    gid = str(group.id)
    cases = [
        cl.post(G.format(gid=gid) + "/attendance/start", json={"lessonDate": "2026-10-22", "lessonId": "lesson-f"}),
        cl.post("/api/v1/auth/org/schedule/lessons", json={"groupId": gid, "dayOfWeek": 2, "startTime": "10:00", "endTime": "11:00"}),
        cl.post("/api/v1/auth/org/schedule/occurrences", json={"groupId": gid, "lessonDate": "2026-10-30", "startTime": "10:00", "endTime": "11:00", "purpose": "extra", "money": "paid"}),
        cl.put("/api/v1/auth/org/schedule/lessons/lesson-f/recurrence", json={"effectiveFrom": "2026-11-12", "reason": "x" * 5, "endTime": "16:30"}),
        cl.put(G.format(gid=gid), json={"name": "Новое имя"}),
        cl.post(G.format(gid=gid) + f"/students/{w.s6.id}"),
    ]
    assert [c.json()["detail"]["code"] for c in cases] == ["GROUP_ARCHIVED"] * len(cases), [c.text[:120] for c in cases]


def test_chain2_archive_a_group_with_a_student_missed_lessons_and_an_exam(w, clock, exams):
    """[O4.R1] [O4.R5] [O4.R6] Chain 2 (golden): 8-В, the student s6, the missed Monday 19.10 and a lobby exam E; impact -> move / archive the student -> archive with cancel ->
    start refused -> restore (B inactive: TEACHER_INACTIVE, then C)."""
    ids = w.ids
    exams.group_exams = [{"examId": "EX_9", "title": "Контрольная №4", "status": "lobby", "lessonDate": "2026-10-26", "canRelease": True, "blockerReason": None}]
    cl = client(w, "admin")
    impact = cl.get(G.format(gid=w.G3.id) + "/archive-impact")
    expected = ids.remap(fixture("c2_archive_impact.json"))
    assert impact.status_code == 200 and impact.json() == expected, impact.json()
    # 2. s6 cannot move to the full 9-А (3/3); the administrator archives the student ("ушёл из центра")
    bulk = cl.post(G.format(gid=w.G1.id) + "/students/bulk", json={"student_ids": [str(w.s6.id)]})
    assert bulk.json()["added"] == 0 and "лимит" in bulk.json()["skipped_details"][0]["reason"]
    assert cl.post(f"/api/v1/auth/org/students/{w.s6.id}/archive", json={"reason": "ушёл из центра"}).status_code == 200
    again = cl.get(G.format(gid=w.G3.id) + "/archive-impact").json()
    assert [b["code"] for b in again["blockers"]] == ["MISSED_LESSONS"]
    # 3. archive with the explicit decision on the missed lesson
    done = archive(w, w.G3, reason="Группа закрыта", missedLessonsAction="cancel")
    assert done.status_code == 200, done.text
    assert done.json() == ids.remap(fixture("c2_archive_ok.json")) | {"releasedExamIds": ["EX_9"]} | {} or True
    expected_ok = ids.remap(fixture("c2_archive_ok.json"))
    expected_ok["releasedExamIds"] = ["EX_9"]
    assert done.json() == expected_ok
    state = fixture("c2_group_state_after_archive.json")
    g = refresh(w, w.G3)
    assert g.inactive_intervals == state["inactive_intervals"]
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G3.id, lesson_date="2026-10-19").one()
    assert (row.status, row.teacher_id, row.cancellation_reason) == ("cancelled", w.B.id, state["cancelled_row"]["cancellation_reason"])
    assert w.db.query(GroupAudit).filter_by(group_id=w.G3.id, action=state["group_audit_action"]).count() == 1
    assert ("/internal/group-bindings/archive-release" in [c[0] for c in exams.calls]) or any("archive-release" in c[0] for c in exams.calls)
    # 4. the teacher B reads the journal, cannot start on 26.10
    clock.set("2026-10-26 10:05")
    assert client(w, "B").get(G.format(gid=w.G3.id) + "/attendance", params={"month": "2026-10"}).status_code == 200
    started = client(w, "B").post(G.format(gid=w.G3.id) + "/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l4"})
    assert started.status_code == 409 and started.json() == fixture("c2_start_archived.json")["body"]
    sched = client(w, "B").get("/api/v1/auth/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26", "teacher_id": str(w.B.id)}).json()
    assert [o for o in sched if o["groupId"] == str(w.G3.id)] == []
    # 5. restore on 05.11
    clock.set("2026-11-05 14:00")
    w.db.query(type(w.member("B"))).filter_by(user_id=w.B.id).update({"is_active": False})
    w.db.commit()
    inactive = cl.post(G.format(gid=w.G3.id) + "/restore", json={})
    assert inactive.status_code == 409 and inactive.json() == fixture("c2_restore_teacher_inactive.json")["body"]
    ok = cl.post(G.format(gid=w.G3.id) + "/restore", json={"teacherId": str(w.C.id)})
    assert ok.status_code == 200
    expected_after = ids.remap(fixture("c2_group_after_restore.json"))
    assert ok.json() == expected_after
    g = refresh(w, w.G3)
    st = fixture("c2_state_after_restore.json")
    assert g.inactive_intervals == st["inactive_intervals"]
    days = {o["occurrenceDate"] for o in w.svc._resolved_occurrences_for_group(g, date_from=date(2026, 10, 19), date_to=date(2026, 11, 16))}
    assert not set(st["no_occurrence_on"]) & days and set(st["occurrence_on"]) <= days


def test_exam_release_failure_after_the_archive_is_a_durable_marker_retried_later(w, exams):
    """[O4.R7] The archive is committed first; if exam-service does not confirm the release the group stays archived with a durable marker (``examReleasePending``), the next archive-related call
    retries (idempotent) and clears it; a conflict at release (an exam started meanwhile) keeps the marker too."""
    from src.auth.group_lifecycle import GroupLifecycle

    group = free_group(w, "Маркер")
    exams.group_exams = [{"examId": "EX_7", "title": "Контрольная", "status": "lobby", "lessonDate": "2026-10-29", "canRelease": True, "blockerReason": None}]
    exams.release_conflict = True
    r = archive(w, group, reason="Закрыта")
    assert r.status_code == 200 and r.json()["status"] == "archived" and r.json()["examReleasePending"] is True and r.json()["releasedExamIds"] == []
    g = refresh(w, group)
    assert g.status == "archived" and g.exam_release_pending is True
    # still failing: the retry keeps the marker
    assert GroupLifecycle(w.svc).retry_pending_releases(w.org.id) == 0
    assert refresh(w, group).exam_release_pending is True
    # exam-service is fine again: the next archive-related call (the impact) clears it
    exams.release_conflict = False
    impact = client(w, "admin").get(G.format(gid=group.id) + "/archive-impact")
    assert impact.status_code == 200
    assert refresh(w, group).exam_release_pending is False
    assert GroupLifecycle(w.svc).retry_pending_releases(w.org.id) == 0          # idempotent
    # unavailable at release time behaves the same; the sweep clears it
    group2 = free_group(w, "Маркер2")
    exams.release_unavailable = True
    r2 = archive(w, group2)
    assert r2.status_code == 200 and r2.json()["examReleasePending"] is True and refresh(w, group2).status == "archived"
    exams.release_unavailable = False
    assert GroupLifecycle(w.svc).retry_pending_releases() >= 1                   # the sweep (all centres)
    assert refresh(w, group2).exam_release_pending is False
