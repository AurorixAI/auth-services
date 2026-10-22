"""LOGIC-B golden chains of the SPEC (section 2) on the data set D0, compared with the fixtures of ``tests/fixtures/logicb`` (chains 2, 3, 6 and 7
live next to their items: test_ops_o4 / o9 / o10 / o7)."""
from datetime import date, datetime

import pytest
from sqlalchemy import func

from src.models.auth import GroupAttendance, GroupAudit, StudentGroup

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, diff, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session,
)

pytestmark = needs_pg
API = "/api/v1/auth"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d0(pg_session)


def base(w, group):
    return f"{API}/org/groups/{group.id}/attendance"


def row_of(w, group, lesson, day):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=group.id, lesson_id=lesson, lesson_date=day).one()


def test_chain1_teacher_removed_groups_handed_over_substitution_payroll(w, clock):
    """Chain 1: 14:00 impact -> handover G1 -> B is busy -> G1 -> C, G2 -> B -> DELETE A; 14:10 the admin reconciles L3 of 20.10 (A taught it); L2 of 21.10 is C's;
    L1 of 26.10 is given to D who teaches it; the October lesson counts per teacher; A stays in the payroll data."""
    ids, owner, admin = w.ids, client(w, "owner"), client(w, "admin")
    mid = lambda key: str(ids("member_" + key))  # noqa: E731
    impact = owner.get(f"{API}/org/members/{mid('A')}/removal-impact").json()
    assert not diff(impact, ids.remap(fixture("c1_removal_impact_A.json"))), diff(impact, ids.remap(fixture("c1_removal_impact_A.json")))
    busy = owner.post(f"{API}/org/members/{mid('A')}/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.B.id)}]})
    assert busy.status_code == 409 and not diff(busy.json(), ids.remap(fixture("c1_handover_busy.json"))["body"]), diff(busy.json(), ids.remap(fixture("c1_handover_busy.json"))["body"])
    ok = owner.post(f"{API}/org/members/{mid('A')}/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]})
    assert not diff(ok.json(), ids.remap(fixture("c1_handover_ok.json")))
    audit_rows = {r.group_id: r for r in w.db.query(GroupAudit).filter_by(action="teacher_changed")}
    for item in fixture("c1_group_audit_handover.json"):
        row = audit_rows[ids(item["group"])]
        assert row.created_at.isoformat() == item["created_at"] and row.snapshot == ids.remap(item["snapshot"])
    assert not diff(owner.delete(f"{API}/org/members/{mid('A')}").json(), fixture("c1_delete_member_A.json"))
    # 14:10 the admin reconciles the lesson of 20.10 that nobody started: the teacher at the END of the lesson (06:00 UTC on 20.10) is A
    clock.set("2026-10-21 14:10")
    assert admin.post(base(w, w.G2) + "/reconcile", json={"lessonDate": "2026-10-20", "lessonId": "lesson-l3"}).json()["version"] == 1
    expected_rows = ids.remap(fixture("c1_attendance_rows.json"))
    r3 = row_of(w, w.G2, "lesson-l3", "2026-10-20")
    assert (str(r3.teacher_id), str(r3.marked_by), r3.started_at) == (expected_rows["L3_2026-10-20"]["teacher_id"], expected_rows["L3_2026-10-20"]["marked_by"], None)
    marks = {str(w.s4.id): "present", str(w.s5.id): "present"}
    assert admin.post(base(w, w.G2), json={"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "status": "completed", "records": marks, "expectedVersion": 1}).status_code == 200
    # Wed 16:00 L2: the end 17:30 is after the handover -> C
    clock.set("2026-10-21 16:05")
    assert client(w, "C").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    l2 = row_of(w, w.G1, "lesson-l2", "2026-10-21")
    assert (str(l2.teacher_id), str(l2.marked_by)) == (expected_rows["L2_2026-10-21"]["teacher_id"], expected_rows["L2_2026-10-21"]["marked_by"])
    clock.set("2026-10-21 17:20")
    g1 = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    assert client(w, "C").post(base(w, w.G1), json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "records": g1, "expectedVersion": 1}).status_code == 200
    # Mon 26.10: the admin gives L1 to D, D teaches it
    put = admin.put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    assert put.status_code == 200 and not diff(put.json(), ids.remap(fixture("c1_lesson_teacher_L1_D.json"))), diff(put.json(), ids.remap(fixture("c1_lesson_teacher_L1_D.json")))
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    clock.set("2026-10-26 11:20")
    done = client(w, "D").post(base(w, w.G1), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "records": g1, "expectedVersion": 1})
    assert done.status_code == 200, done.text
    l1 = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (str(l1.teacher_id), str(l1.marked_by)) == (expected_rows["L1_2026-10-26"]["teacher_id"], expected_rows["L1_2026-10-26"]["marked_by"])
    # 7. the completed lessons of October per teacher (payroll reads group_attendance.teacher_id); the admin has none (INV-O5-4)
    counts = dict(w.db.query(GroupAttendance.teacher_id, func.count(GroupAttendance.id)).filter(
        GroupAttendance.org_id == w.org.id, GroupAttendance.status == "completed", GroupAttendance.lesson_date >= "2026-10-01",
        GroupAttendance.lesson_date < "2026-11-01").group_by(GroupAttendance.teacher_id).all())
    expected = fixture("c1_expected_lessons_count.json")
    by_key = {"A": w.A, "B": w.B, "C": w.C, "D": w.D, "admin": w.admin}
    assert {k: counts.get(u.id, 0) for k, u in by_key.items()} == expected
    # 8. A is no longer an active member but his lessons stay his
    assert w.member("A").is_active is False and counts[w.A.id] == 5


def test_chain4_phantom_lessons_the_seven_day_window_and_a_missed_lesson_after_a_handover(w, clock):
    """Chain 4: a slot added today has no past lessons (reconcile 19.10 refused); a lesson missed on 13.10 belongs to A (the teacher of that date) although the group went to B on 21.10 and A
    was removed: the administrator reconciles it, B does not (NOT_LESSON_TEACHER), and a teacher would not be allowed to close a lesson older than 7 days anyway."""
    ids, admin = w.ids, client(w, "admin")
    g5 = new_group(w.db, w.org, ids, "G5", "10-А", w.D, [], class_level=10)
    new_student(w.db, w.org, ids, "g5a", "Пятый", "Ученик", g5, class_level=10, joined=datetime(2026, 10, 1, 5, 0))
    w.db.commit()
    created = admin.post(f"{API}/org/schedule/lessons", json={"groupId": str(g5.id), "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:30", "room": None}).json()
    golden = ids.remap(fixture("c4_schedule_lesson_created.json"))
    golden["id"] = created["id"]
    golden["groupId"] = str(g5.id)
    golden["classLevel"] = 10
    assert not diff(created, golden), diff(created, golden)
    assert [a for a in admin.get(f"{API}/org/attendance/open-actions").json() if a["groupId"] == str(g5.id)] == []
    refused = admin.post(base(w, g5) + "/reconcile", json={"lessonDate": "2026-10-19", "lessonId": created["id"]})
    assert refused.status_code == 400 and refused.json() == fixture("c4_reconcile_not_in_schedule.json")["body"]
    # the group of A: Tuesday 13.10 was never started, on 21.10 14:00 the groups go to B and A is removed
    w.db.query(GroupAttendance).filter_by(group_id=w.G2.id, lesson_date="2026-10-13").delete()
    w.db.commit()
    owner = client(w, "owner")
    assert owner.post(f"{API}/org/members/{ids('member_A')}/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]}).status_code == 200
    assert owner.delete(f"{API}/org/members/{ids('member_A')}").status_code == 200
    clock.set("2026-10-21 15:00")
    b = client(w, "B").post(base(w, w.G2) + "/reconcile", json={"lessonDate": "2026-10-13", "lessonId": "lesson-l3"})
    assert b.status_code == 403 and b.json() == fixture("c4_not_lesson_teacher.json")["body"]
    ok = admin.post(base(w, w.G2) + "/reconcile", json={"lessonDate": "2026-10-13", "lessonId": "lesson-l3"})
    assert ok.status_code == 200
    row = row_of(w, w.G2, "lesson-l3", "2026-10-13")
    assert (row.teacher_id, row.marked_by) == (w.A.id, w.admin.id)


def test_chain5_the_running_exam_after_a_transfer_o17_auth_side(w, clock):
    """[O17.R1] [O17.R2] [O17.R3] [O17.R4] [O17.R5] [O17.R6] [O17.R7] [O17.R8] [O17.R9] Chain 5 / O17 (auth side; the exam-service side is in its test_o17_resume_after_transfer): s2 was a member of 9-А when the exam began (as_of 16:45 local = 11:45 UTC); he is moved to 7-Б after the lesson: the ordinary check says no, the check as of the start says yes
    (the golden answer); the boundary rows: left_at == started_at is not enrolled, moved before the start is not enrolled, archived during the exam is enrolled as of the start."""
    clock.set("2026-10-21 17:20")
    as_of = datetime(2026, 10, 21, 11, 45)
    assert client(w, "admin").post(f"{API}/org/groups/{w.G2.id}/students/{w.s2.id}").status_code == 200       # transfer: closes the membership of 9-А at 12:20 UTC
    plain = w.svc.verify_group_student(str(w.G1.id), student_id=w.s2.display_id)
    assert plain["enrolled"] is False
    then = w.svc.verify_group_student(str(w.G1.id), student_id=w.s2.display_id, as_of=as_of)
    golden = fixture("c5_verify_student_as_of.json")
    assert not diff(then, w.ids.remap(golden)), diff(then, w.ids.remap(golden))
    # moved BEFORE the start: not enrolled as of the start
    assert w.svc.verify_group_student(str(w.G1.id), student_id=w.s2.display_id, as_of=datetime(2026, 10, 21, 12, 30))["enrolled"] is False
    # left_at == the instant: not a member (left_at > instant is false)
    membership = w.db.query(StudentGroup).filter_by(student_id=w.s2.id, group_id=w.G1.id).one()
    assert w.svc.verify_group_student(str(w.G1.id), student_id=w.s2.display_id, as_of=membership.left_at)["enrolled"] is False
    # archived during the exam: enrolled as of the start
    archived = w.s3
    client(w, "admin").post(f"{API}/org/students/{archived.id}/archive", json={"reason": "ушёл"})
    assert w.svc.verify_group_student(str(w.G1.id), student_id=archived.display_id)["enrolled"] is False
    assert w.svc.verify_group_student(str(w.G1.id), student_id=archived.display_id, as_of=as_of)["enrolled"] is True
    # the router: an internal call with as_of
    r = client(w, "admin").get(f"{API}/public/groups/{w.G1.id}/verify-student", params={"student_id": w.s2.display_id, "as_of": "2026-10-21T11:45:00"},
                               headers={"X-Internal-Service-Token": "ci-tests-only-internal-token"})
    assert r.status_code == 200 and r.json()["enrolled"] is True
