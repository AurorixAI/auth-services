"""Wave C, package C-A: the hot read paths do not grow with the size of the centre (LESSONS 81), and give the SAME answer as before.

Every test builds a small centre and a big one (30-40 groups) and asserts that the number of SQL statements of the path is the same;
where a path was rewritten, the answer is compared with the answer of the untouched single-row path (the old behaviour).
"""
from uuid import uuid4

import pytest

from tests.pg_world import (  # noqa: F401
    capture_queries, count_queries, make_client, make_group, make_member, make_student, make_user, needs_pg, pg_session, world,
)

pytestmark = needs_pg

API = "/api/v1/auth"


def grow_centre(w, groups=40, per=3, teachers=5):
    """Add ``groups`` groups of ``per`` students; their teachers are spread over ``teachers`` people (the old ``world`` teacher among them)."""
    pool = [w.teacher, w.teacher2] + [make_user(w.db, "teacher", f"Учитель{i}") for i in range(max(0, teachers - 2))]
    for user in pool[2:]:
        make_member(w.db, w.org, user, "teacher")
    made = []
    for g in range(groups):
        group = make_group(w.db, w.org, pool[g % len(pool)], f"Группа {g:02d}")
        for s in range(per):
            make_student(w.db, w.org, group, f"Имя{g}-{s}", f"Фамилия{g}-{s}")
        made.append(group)
    w.db.commit()
    return made


def test_the_counter_helper_counts_and_captures(world):
    n, rows = count_queries(world, lambda: world.db.execute(__import__("sqlalchemy").text("select 1")).all())
    assert n == 1 and rows
    statements, _ = capture_queries(world.db, lambda: world.db.execute(__import__("sqlalchemy").text("select 2")))
    assert statements == ["select 2"]


# ---- SV-08: GET /org/groups -------------------------------------------------------------------------------------------------------------------


def groups_json(w, user=None):
    r = make_client(w, user=user or w.admin).get(f"{API}/org/groups")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv08_org_groups_queries_do_not_grow_with_groups(world):
    small, _ = count_queries(world, lambda: groups_json(world))
    grow_centre(world, groups=40)
    big, body = count_queries(world, lambda: groups_json(world))
    print(f"[wca] SV-08 GET /org/groups: 1 group {small} SQL, 41 groups {big} SQL")
    assert len(body) == 41 and big == small <= 6


def test_sv08_org_groups_answer_equals_the_single_group_answer(world):
    made = grow_centre(world, groups=8)
    made[0].teacher_id = None                       # a group without a teacher
    world.students[0].archived_at = None
    from src.models.auth import StudentGroup

    world.db.query(StudentGroup).filter_by(student_id=world.students[1].id).update({"is_active": False})   # a left student is not counted
    world.db.commit()
    body = groups_json(world)
    expected = [world.svc._build_group_response(g).model_dump(mode="json") for g in
                sorted([world.group, *made], key=lambda g: g.name)]
    assert [dict(item) for item in body] == [dict(item) for item in expected]
    assert next(g for g in body if g["id"] == str(world.group.id))["student_count"] == 2
    assert next(g for g in body if g["id"] == str(made[0].id))["teacher_name"] is None


def test_sv08_my_groups_of_a_teacher_do_not_grow_and_keep_the_flag(world):
    grow_centre(world, groups=2, teachers=2)
    small, small_body = count_queries(world, lambda: groups_json(world, world.teacher))
    grow_centre(world, groups=40, teachers=2)
    big, body = count_queries(world, lambda: groups_json(world, world.teacher))
    print(f"[wca] SV-08 my groups: {len(small_body)} groups {small} SQL, {len(body)} groups {big} SQL")
    assert len(body) > len(small_body) + 15 and big == small
    assert all(item["substitution"] is False and item["teacher_name"] == body[0]["teacher_name"] for item in body)


# ---- SV-09: GET /org/schedule -------------------------------------------------------------------------------------------------------------------


def revise_schedules(w, groups):
    """Every group gets: a revision of s1 (applied) and one from the far future (not applied), a stopped s2, an s3 that stops in the far future."""
    from src.models.auth import GroupScheduleOverride

    for group in groups:
        def add(lesson, scope, day, payload):
            w.db.add(GroupScheduleOverride(org_id=w.org.id, group_id=group.id, lesson_id=lesson, scope=scope, effective_date=day,
                                           payload=payload, reason="тест", changed_by=w.admin.id))
        add("s1", "recurring", "2026-01-05", {"dayOfWeek": 1, "startTime": "15:00", "endTime": "16:00", "format": "online"})
        add("s1", "recurring", "2999-01-01", {"dayOfWeek": 1, "startTime": "18:00", "endTime": "19:00", "format": "offline"})
        add("s2", "recurring_end", "2026-01-05", {})
        add("s3", "recurring_end", "2999-01-01", {})
    w.db.commit()


def schedule_json(w):
    r = make_client(w).get(f"{API}/org/schedule")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv09_schedule_queries_do_not_grow_with_groups(world):
    revise_schedules(world, [world.group])
    small, _ = count_queries(world, lambda: schedule_json(world))
    made = grow_centre(world, groups=40)
    revise_schedules(world, made)
    big, body = count_queries(world, lambda: schedule_json(world))
    print(f"[wca] SV-09 GET /org/schedule: 1 group {small} SQL, 41 groups {big} SQL")
    assert len({item["groupId"] for item in body}) == 41 and big == small


def test_sv09_schedule_answer_equals_the_per_group_query_answer(world, monkeypatch):
    made = grow_centre(world, groups=6)
    revise_schedules(world, [world.group, *made[:4]])           # the rest has no overrides at all
    body = schedule_json(world)
    assert any(item["startTime"] == "15:00" for item in body)                                  # the revision is applied ...
    assert not any(item["startTime"] == "18:00" for item in body)                              # ... the future one is not
    assert not any(item["id"] == "s2" for item in body if item["groupId"] == str(world.group.id))   # a stopped recurrence is omitted
    assert any(item["id"] == "s3" for item in body if item["groupId"] == str(world.group.id))       # one that stops later is still there
    original = type(world.svc)._schedule_overrides_for_group

    def per_group(self, group, *, until, extra_lesson_ids=None, preloaded_overrides=None):
        return original(self, group, until=until, extra_lesson_ids=extra_lesson_ids)       # the OLD path: one query per group

    monkeypatch.setattr(type(world.svc), "_schedule_overrides_for_group", per_group)
    assert schedule_json(world) == body


# ---- SV-10: a student's attendance reads only the groups he was in --------------------------------------------------------------------------------


def add_lessons(w, group, student_ids, day="2026-09-14", lessons=3, mark="present", snapshot=True):
    """``lessons`` completed lessons of the group on consecutive days; ``student_ids`` are in the frozen roster (and marked ``mark``)."""
    from datetime import date, timedelta

    from src.models.auth import GroupAttendance

    first = date.fromisoformat(day)
    for i in range(lessons):
        w.db.add(GroupAttendance(
            org_id=w.org.id, group_id=group.id, lesson_date=(first + timedelta(days=i)).isoformat(), lesson_id=f"L{i}", topic=f"Тема {i}", status="completed",
            records={str(s): mark for s in student_ids},
            roster_snapshot=[{"student_id": str(s), "name": "x"} for s in student_ids] if snapshot else None,
            marked_by=w.teacher.id, teacher_id=group.teacher_id,
        ))
    w.db.commit()


def student_attendance(w, student, month="2026-09"):
    r = make_client(w).get(f"{API}/org/students/{student.id}/attendance?month={month}")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv10_student_attendance_reads_only_his_groups(world):
    me = world.students[0]
    others = grow_centre(world, groups=30)
    add_lessons(world, world.group, [me.id, world.students[1].id], lessons=4)
    for g in others:
        add_lessons(world, g, [], lessons=6)                                   # a big centre: 30 groups x 6 lessons of strangers
    statements, body = capture_queries(world, lambda: student_attendance(world, me), rows=True)
    journal = [(s, n) for s, n in statements if "FROM group_attendance JOIN groups" in s]       # the month's journal
    print(f"[wca] SV-10 journal rows read for a student of a 31-group centre: {[n for _, n in journal]}")
    assert len(journal) == 1 and journal[0][1] == 4                            # only the 4 lessons of HIS group, not 4 + 180
    assert len(body["lessons"]) == 4


def test_sv10_student_attendance_answer_keeps_closed_groups_and_other_rosters(world):
    from src.models.auth import StudentGroup

    me, other = world.students[0], world.students[1]
    old_group = make_group(world.db, world.org, world.teacher2, "Старая группа")
    world.db.add(StudentGroup(student_id=me.id, group_id=old_group.id, is_active=False, joined_at=__import__("tests.pg_world", fromlist=["x"]).ENROLLED_SINCE))
    world.db.commit()
    add_lessons(world, old_group, [me.id], day="2026-09-01", lessons=2, mark="absent")        # a group he has LEFT is still his history
    add_lessons(world, world.group, [me.id], day="2026-09-10", lessons=3, mark="late")
    add_lessons(world, world.group, [other.id], day="2026-09-20", lessons=2, mark="present")  # lessons without him in the roster are not his (other lesson ids)
    add_lessons(world, world.group, [me.id], day="2026-09-25", lessons=1, mark="present", snapshot=False)   # a legacy row: no frozen roster, he is in the records
    body = student_attendance(world, me)
    assert [item["date"] for item in body["lessons"]] == ["2026-09-01", "2026-09-02", "2026-09-10", "2026-09-11", "2026-09-12", "2026-09-25"]
    assert {g["group_name"] for g in body["groups"]} == {"Старая группа", world.group.name}


# ---- SV-11: the group page does not read the heavy JSON of the diagnostic results ----------------------------------------------------------------


HEAVY = ("identified_gaps", "root_gaps", "recommended_path", "error_summary", "integrity_report")


def add_result(w, student, theta, ago_days, invite=None, level=None):
    from datetime import datetime, timedelta

    from src.models.auth import StudentDiagnosticResult

    row = StudentDiagnosticResult(
        student_id=student.id, org_id=w.org.id, invite_id=invite.id if invite else None, final_theta=theta, accuracy=0.5, total_tasks=20, correct_answers=10,
        overall_level=level, diagnosed_at=datetime(2026, 10, 1, 12, 0) - timedelta(days=ago_days),
        identified_gaps=[{"skill": f"s{i}", "note": "x" * 200} for i in range(40)], root_gaps=[{"a": "y" * 300}] * 10,
        error_summary={"k": "z" * 500}, integrity_report={"r": "q" * 500},
    )
    w.db.add(row)
    w.db.commit()
    return row


def test_sv11_group_page_reads_one_light_row_per_student(world):
    me = world.students
    for i, s in enumerate(me):
        add_result(world, s, -1.0 + i, ago_days=60)           # old results ...
        add_result(world, s, 1.0 + i, ago_days=30)
        latest = add_result(world, s, 2.0 + i, ago_days=1, level="at_grade")     # ... and the latest one
    world.db.expire_all()
    statements, r = capture_queries(world, lambda: make_client(world).get(f"{API}/org/groups/{world.group.id}"), rows=True)
    assert r.status_code == 200
    reads = [(s, n) for s, n in statements if "FROM student_diagnostic_results" in s]
    print(f"[wca] SV-11 group page: diagnostic result rows read {[n for _, n in reads]} (9 stored)")
    assert len(reads) == 1 and reads[0][1] == 3                                   # 3 students, 9 results stored -> 3 rows
    assert not any(col in reads[0][0] for col in HEAVY)
    students = {item["id"]: item for item in r.json()["students"]}
    for i, s in enumerate(me):
        assert students[str(s.id)]["last_theta"] == 2.0 + i                       # the latest, not the first
        assert students[str(s.id)]["last_level"] == ("at_grade" if i == 2 else students[str(s.id)]["last_level"])
    assert latest.overall_level == "at_grade"


def test_sv11_group_stats_and_invite_list_use_the_light_rows_with_the_same_answer(world):
    from datetime import datetime, timedelta

    from src.models.auth import DiagnosticInvite

    s0, s1, s2 = world.students
    add_result(world, s0, -1.0, 40)
    add_result(world, s0, 0.9, 5)
    add_result(world, s1, 0.4, 3)
    invite = DiagnosticInvite(org_id=world.org.id, student_id=s0.id, created_by=world.admin.id, code=uuid4().hex[:10].upper(), class_level=9, status="completed",
                              expires_at=datetime.utcnow() + timedelta(days=3))
    world.db.add(invite)
    world.db.commit()
    add_result(world, s0, 0.2, 90, invite=invite)
    world.db.expire_all()
    statements, r = capture_queries(world, lambda: make_client(world).get(f"{API}/org/groups/{world.group.id}/stats"), rows=True)
    body = r.json()
    assert body["diagnosed_count"] == 2 and abs(body["avg_theta"] - (0.9 + 0.4) / 2) < 1e-9      # the latest of s0 (0.9), s1 (0.4); s2 has none
    reads = [s for s, _ in statements if "FROM student_diagnostic_results" in s]
    assert len(reads) == 1 and not any(col in reads[0] for col in HEAVY)
    world.db.expire_all()
    statements, r = capture_queries(world, lambda: make_client(world).get(f"{API}/org/diagnostics"), rows=True)
    item = next(i for i in r.json() if i["id"] == str(invite.id))
    assert item["result"]["theta"] == 0.2                                                           # the result of THIS invite, not the latest of the student
    reads = [s for s, _ in statements if "FROM student_diagnostic_results" in s]
    assert len(reads) == 1 and not any(col in reads[0] for col in HEAVY)


# ---- DM-009: «Замены» of a teacher ----------------------------------------------------------------------------------------------------------------

from tests.b2_helpers import build_d2, client as sub_client, decide, exams, clock, new_group, new_slot, req as sub_req  # noqa: E402,F401

SUB = f"{API}/org/substitutions"


@pytest.fixture
def sw(pg_session, clock, exams):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def grow_subs(w, start, pending, assigned):
    """``pending`` groups of A whose Monday-to-Saturday lesson A asks to be covered (stays pending) and ``assigned`` such lessons the admin gives to D."""
    from tests.b2_helpers import new_student

    made = []
    for i in range(start, start + pending + assigned):
        dow, hour = 1 + i % 6, 8 + i // 6
        day = f"2026-10-{25 + dow}"
        group = new_group(w.db, w.org, w.ids, f"WC{i}", f"WC-{i}", w.A, [new_slot(f"wc-lesson-{i}", dow, f"{hour:02d}:00", f"{hour:02d}:45", "2026-10-12")])
        new_student(w.db, w.org, w.ids, f"wcs{i}", f"Фам{i}", f"Имя{i}", group)
        made.append((group, f"wc-lesson-{i}", day))
    w.db.commit()
    for k, lesson in enumerate(made):
        r = sub_req(w, "A", [lesson])
        assert r.status_code == 201, r.text
        if k >= pending:
            assert decide(w, "admin", r.json()["created"][0]["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    return made


def test_dm009_mine_queries(sw):
    grow_subs(sw, 0, 2, 2)
    small_a, ra = count_queries(sw, lambda: sub_client(sw, "A").get(f"{SUB}/mine"))
    small_d, rd = count_queries(sw, lambda: sub_client(sw, "D").get(f"{SUB}/mine"))
    grow_subs(sw, 4, 10, 10)
    big_a, ra2 = count_queries(sw, lambda: sub_client(sw, "A").get(f"{SUB}/mine"))
    big_d, rd2 = count_queries(sw, lambda: sub_client(sw, "D").get(f"{SUB}/mine"))
    print(f"[wca] DM-009 mine (teacher A: {len(ra.json()['items'])} -> {len(ra2.json()['items'])} requests): {small_a} -> {big_a} SQL; "
          f"substitute D: {len(rd.json()['assignedToMe'])} -> {len(rd2.json()['assignedToMe'])} lessons: {small_d} -> {big_d} SQL")
    assert (big_a, big_d) == (small_a, small_d)


def test_dm009_prefetched_answers_equal_the_per_request_answers(sw, monkeypatch):
    """The batch (``prefetch``, the preloaded schedule) gives the same reason for every pending request and the same ``mine`` for the teacher and for the substitute as the per-request queries."""
    from sqlalchemy import select as sa_select

    from src.auth.lesson_substitutions import Substitutions
    from src.auth.lesson_teachers import TeacherBook
    from src.auth.service import AuthService
    from src.models.auth import GroupAttendance, LessonSubstitutionRequest, OrganizationMember

    made = grow_subs(sw, 0, 8, 3)
    # B asks too (a lesson of the group G3 on Monday); then B stops working in the centre
    b_req = sub_req(sw, "B", [(sw.G3, "lesson-l4", "2026-10-26")])
    assert b_req.status_code == 201, b_req.text
    sw.db.query(OrganizationMember).filter_by(org_id=sw.org.id, user_id=sw.B.id).update({"is_active": False})

    def attendance(group, lesson, day, status):
        sw.db.add(GroupAttendance(org_id=sw.org.id, group_id=group.id, lesson_id=lesson, lesson_date=day, status=status, records={}, roster_snapshot=[], marked_by=sw.A.id))

    attendance(*made[0], "cancelled")                           # LESSON_CANCELLED
    attendance(*made[1], "in_progress")                         # LESSON_STARTED
    made[2][0].teacher_id = sw.C.id                             # GROUP_TEACHER_CHANGED
    made[3][0].status = "archived"                              # GROUP_ARCHIVED
    slot = dict(made[4][0].schedule[0], startTime="09:30", endTime="10:15")
    made[4][0].schedule = [slot]                                # the lesson only changed its time: the request follows
    made[5][0].schedule = [dict(made[5][0].schedule[0], id="other-lesson")]       # LESSON_REMOVED
    attendance(*made[9], "cancelled")                           # an assigned lesson that was cancelled is not listed for the substitute
    sw.db.commit()

    subs = Substitutions(AuthService(sw.db))
    pending = sw.db.query(LessonSubstitutionRequest).filter_by(org_id=sw.org.id, status="pending").all()
    assert len(pending) == 7                # the two with a cancelled / started lesson were closed by the writer of that row at once
    groups = {g.id: g for g in sw.db.query(type(sw.G1)).filter(type(sw.G1).id.in_({r.group_id for r in pending})).all()}
    book = TeacherBook(sw.db)
    pre = subs.prefetch(pending, book)
    for r in pending:
        assert subs.closure_reason(r, groups[r.group_id], book, pre) == subs.closure_reason(r, groups[r.group_id]), r.lesson_id
    reasons = {subs.closure_reason(r, groups[r.group_id])[0] for r in pending}
    assert {"GROUP_TEACHER_CHANGED", "GROUP_ARCHIVED", "LESSON_REMOVED", "TEACHER_REMOVED", None} <= reasons

    first = {who: sub_client(sw, who).get(f"{SUB}/mine").json() for who in ("A", "D")}        # the new path closes what has to be closed ...
    closed = {item["lesson"]["lessonId"]: item["decision"]["closedReason"] for item in first["A"]["items"] if item["status"] == "closed"}
    assert len(closed) == 5
    original = AuthService._resolved_occurrences_for_group

    def per_group(self, group, *, date_from, date_to, preloaded=None):
        return original(self, group, date_from=date_from, date_to=date_to)                 # the OLD path: two queries per call

    monkeypatch.setattr(AuthService, "_resolved_occurrences_for_group", per_group)
    monkeypatch.setattr(Substitutions, "prefetch", lambda self, reqs, book: None)
    second = {who: sub_client(sw, who).get(f"{SUB}/mine").json() for who in ("A", "D")}       # ... and the per-request path reads the same from the settled state
    assert second == first
    assert len(first["D"]["assignedToMe"]) == 2                                              # three given, one of them cancelled


# ---- SV-12: one scan for the group of a recurring lesson ------------------------------------------------------------------------------------------


def test_sv12_both_flows_use_the_one_lookup_and_keep_their_messages(world):
    from src.auth.schemas import ReviseRecurringLessonRequest, UpdateLessonRequest

    org = str(world.org.id)
    made = grow_centre(world, groups=3)
    assert world.svc._group_by_lesson_id(org, "s1").id in {world.group.id, *[g.id for g in made]}          # the same lesson id in every group: the first found
    assert world.svc._group_by_lesson_id(org, "no-such-lesson") is None
    assert world.svc._group_by_lesson_id(str(world.other_org.id), "s1").id == world.foreign_group.id        # only the groups of the own centre
    statements, _ = capture_queries(world, lambda: world.svc._group_by_lesson_id(org, "s1"))
    assert len(statements) == 1
    with pytest.raises(ValueError, match="Регулярное занятие не найдено"):
        world.svc._require_recurring_lesson(org, "no-such-lesson", actor_id=str(world.admin.id), is_org_admin=True)
    assert world.svc._require_recurring_lesson(org, "s1", actor_id=str(world.admin.id), is_org_admin=True).org_id == world.org.id
    with pytest.raises(ValueError, match="Урок не найден"):
        world.svc.revise_recurring_lesson(org, "no-such-lesson", ReviseRecurringLessonRequest.model_construct(), actor_id=str(world.admin.id), is_org_admin=True)
    with pytest.raises(ValueError, match="Прямое редактирование расписания отключено"):
        world.svc.update_lesson(org, "s1", UpdateLessonRequest.model_construct(), actor_id=str(world.admin.id), is_org_admin=True)


# ---- SV-22: the sign-in asks for the centre and the role of the user once ---------------------------------------------------------------------------


def test_sv22_login_reads_the_centre_once_and_answers_the_same(world):
    from jose import jwt

    from src.auth.schemas import LoginRequest
    from src.auth.service import hash_password

    world.admin.password_hash = hash_password("wave-c-password-1")
    world.admin.default_org_id = world.org.id
    world.db.commit()

    def sign_in():
        return world.svc.login(LoginRequest(email=world.admin.email, password="wave-c-password-1"))

    statements, answer = capture_queries(world, sign_in)
    print(f"[wca] SV-22 login: {len(statements)} SQL")
    assert len(statements) == 18                                # was 21: the membership and the centre are read once, not three times
    claims = jwt.get_unverified_claims(answer.tokens.access_token)
    assert (claims["org_id"], claims["org_role"]) == (str(world.org.id), "admin")
    assert answer.user.org_role == "admin" and answer.user.org_name == world.org.name
    assert answer.org.id == str(world.org.id) and answer.org.name == world.org.name


# ---- SV-23: the dashboard counts in one round trip ------------------------------------------------------------------------------------------------


def test_sv23_org_stats_one_round_trip_and_the_same_numbers(world):
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from src.models.auth import DiagnosticInvite, Group, OrganizationMember, Student

    made = grow_centre(world, groups=5, per=2)
    made[0].status = "archived"
    world.db.query(Student).filter_by(group_id=made[1].id).update({"status": "active"})
    world.db.query(Student).filter_by(group_id=made[2].id).update({"status": "archived"})
    world.db.query(OrganizationMember).filter_by(org_id=world.org.id, user_id=world.teacher2.id).update({"is_active": False})
    for status in ("pending", "confirmed", "in_progress", "completed", "completed", "cancelled", "expired"):
        world.db.add(DiagnosticInvite(org_id=world.org.id, student_id=world.students[0].id, created_by=world.admin.id, code=uuid4().hex[:10].upper(), class_level=9,
                                      status=status, expires_at=datetime.utcnow() + timedelta(days=3)))
    world.db.commit()
    org = world.org.id
    expected = {                                           # the numbers of the six separate queries this endpoint used to run
        "total_students": world.db.query(func.count(Student.id)).filter(Student.org_id == org, Student.status != "archived").scalar(),
        "total_groups": world.db.query(func.count(Group.id)).filter(Group.org_id == org, Group.status == "active").scalar(),
        "total_members": world.db.query(func.count(OrganizationMember.id)).filter(OrganizationMember.org_id == org, OrganizationMember.is_active).scalar(),
        "active_diagnostics": 3, "completed_diagnostics": 2,
        "new_students": world.db.query(func.count(Student.id)).filter(Student.org_id == org, Student.status == "new").scalar(),
    }
    assert expected["new_students"] == 9 and expected["total_groups"] == 5 and expected["total_students"] == 3 + 10 - 2
    statements, body = capture_queries(world, lambda: make_client(world, user=world.owner).get(f"{API}/org/stats").json())
    print(f"[wca] SV-23 GET /org/stats: {len(statements)} SQL")
    assert {k: body[k] for k in expected} == expected
    counting = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "count(" in s]
    assert len(counting) == 2                              # the six counts in one statement + the students without a group


# ---- SV-25: the student card reads the student once ---------------------------------------------------------------------------------------------


def test_sv25_student_profile_reads_the_student_once_for_owner_and_teacher(world):
    me = world.students[0]
    add_result(world, me, 0.7, ago_days=3)
    url = f"{API}/org/students/{me.id}/profile"
    seen = {}
    for who in ("admin", "teacher"):
        user = getattr(world, who)
        world.db.expire_all()
        statements, r = capture_queries(world, lambda: make_client(world, user=user).get(url))
        assert r.status_code == 200, r.text
        reads = [s for s in statements if s.lstrip().startswith("SELECT students.") or " FROM students " in s]
        print(f"[wca] SV-25 profile as {who}: {len(statements)} SQL, {len(reads)} read the student")
        assert len(reads) == 1
        seen[who] = r.json()
    body = seen["admin"]
    assert body["group"]["id"] == str(world.group.id) and body["group"]["teacher_id"] == str(world.teacher.id)
    assert body["group"]["teacher_name"] == f"{world.teacher.first_name} {world.teacher.last_name}".strip()
    assert seen["teacher"]["group"] == body["group"]
    assert make_client(world, user=world.teacher2).get(url).status_code == 403       # another teacher: still refused
    assert make_client(world).get(f"{API}/org/students/{uuid4()}/profile").status_code in (400, 404)


# ---- SV-24: the dashboard of a teacher (counter first: the cost must not grow with his groups) --------------------------------------------------------


def dashboard_json(w, user):
    r = make_client(w, user=user).get(f"{API}/org/teacher/dashboard/summary?business_date=2026-10-26")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv24_teacher_dashboard_queries_do_not_grow_with_his_groups(world):
    small, small_body = count_queries(world, lambda: dashboard_json(world, world.teacher))
    grow_centre(world, groups=24, teachers=2)
    big, body = count_queries(world, lambda: dashboard_json(world, world.teacher))
    print(f"[wca] SV-24 teacher dashboard: {len(small_body['groups'])} groups {small} SQL, {len(body['groups'])} groups {big} SQL")
    assert len(body["groups"]) > len(small_body["groups"]) + 10 and body["studentCount"] > small_body["studentCount"]
    assert big == small


# ---- DM-012: the callback «диагностика завершена» reads the skill states in one query ---------------------------------------------------------------


def test_dm012_skill_states_one_select_for_any_number_of_topics_same_outcome(world):
    from datetime import datetime

    from src.auth.diagnostic_skill_states import apply_skill_states
    from src.models.analytics import StudentSkillState

    NOW, LATER = datetime(2026, 10, 1, 12, 0), datetime(2026, 10, 1, 12, 5)
    sid = world.students[0].id

    def run(topics, now, result_at=None):
        breakdown = {f"T{i}": {"alpha": 0.9 if i % 2 else -1.0} for i in range(topics)}
        breakdown["bad"] = "not a dict"                                   # skipped
        statements, counts = capture_queries(world, lambda: apply_skill_states(
            world.db, student_id=str(sid), org_id=str(world.org.id), theta=0.4, topic_breakdown=breakdown, gaps=set(), now=now, result_at=result_at))
        return [s for s in statements if "FROM student_skill_states" in s], counts

    reads5, first5 = run(5, NOW)
    reads40, first40 = run(40, NOW)                                       # 5 known (replay of the same values) + 35 new
    print(f"[wca] DM-012 apply_skill_states: 5 topics {len(reads5)} select, 40 topics {len(reads40)} select")
    assert (len(reads5), len(reads40)) == (1, 1)
    assert first5 == {"written": 5, "unchanged": 0, "skipped": 1, "stale": 0}
    assert first40 == {"written": 35, "unchanged": 5, "skipped": 1, "stale": 0}
    _, stale = run(40, LATER, result_at=datetime(2026, 9, 1))              # an older result than the stored one: never rolls back
    assert stale == {"written": 0, "unchanged": 0, "skipped": 1, "stale": 40}
    world.db.expire_all()
    assert world.db.query(StudentSkillState).filter_by(student_id=sid).count() == 40
    assert float(world.db.query(StudentSkillState).filter_by(student_id=sid, skill_id="T1").one().mastery_probability) == 0.725


# ---- MN-05 / MN-16: the payments summary selects once; a page of receipts runs only the memberships of its records -----------------------------------

from datetime import date as _date, datetime as _datetime  # noqa: E402

from tests.ops_helpers import build_org  # noqa: E402
from tests.payf_helpers import centre, client as pay_client, pay as pay_row  # noqa: E402


def money_centre(w, clock, groups=6, **block):
    clock.set("2026-10-21 12:00")
    gs, ss, ms = centre(w, groups=groups, joined=_datetime(2026, 9, 1, 6, 0), **block)
    return gs, ss, ms


def test_mn05_summary_selects_the_memberships_once_with_the_same_numbers(pg_session, clock):
    from src.auth import ledger_service as ls, payment_status as ps, payments_service
    from src.models.auth import Group, Student, StudentGroup

    w = build_org(pg_session)
    gs, ss, ms = money_centre(w, clock)
    for m in ms[:4]:
        pay_row(w, m, 600_000, _date(2026, 10, 1))
    ss[4].status = "archived"                                # an archived student: in "all" (he still has an open stay), not tracked
    gs[5].status = "archived"                                # an archived group: the same
    ms[3].is_active = False                                  # left, with money: in "all", not tracked
    ms[3].left_at = _datetime(2026, 10, 10, 6, 0)
    w.db.commit()
    payments_service.invalidate_summary()
    statements, r = capture_queries(w, lambda: pay_client(w, "owner").get("/api/v1/auth/org/payments/summary"))
    assert r.status_code == 200, r.text
    body = r.json()
    selects = [s for s in statements if " ".join(s.split()).startswith("SELECT student_groups.id AS student_groups_id FROM student_groups JOIN")]
    print(f"[wca] MN-05 summary: {len(statements)} SQL, {len(selects)} membership selects")
    assert len(selects) == 1
    loader = ls.LedgerLoader(w.db, str(w.org.id), ps.business_today())
    tracked = loader.select(scope="tracked")                 # the unchanged path: the oracle
    kinds = [e.ledger.status.status for e in loader.run(tracked)]
    assert body["tracked_total"] == len(tracked) == 3
    assert body["expired"] == kinds.count(ps.EXPIRED) and body["ends_today"] == kinds.count(ps.ENDS_TODAY) and body["none"] == kinds.count(ps.NONE)
    assert body["active"] == kinds.count(ps.ACTIVE) + kinds.count(ps.FREE)
    everything = loader.run(loader.select(scope="all"))
    assert len(everything) == 6
    assert body["debt_total"] == sum(e.ledger.debt for e in everything) and body["advance_total"] == sum(e.ledger.advance for e in everything)


def test_mn16_receipts_items_run_only_the_memberships_of_the_page(pg_session, clock):
    from src.auth import payments_service

    w = build_org(pg_session)
    gs, ss, ms = money_centre(w, clock, groups=8)
    for m in ms:
        pay_row(w, m, 600_000, _date(2026, 9, 5))            # September: every student paid
    for m in ms[:2]:
        pay_row(w, m, 600_000, _date(2026, 10, 5))           # October: two of them again
    own = pay_client(w, "owner")
    url = "/api/v1/auth/org/payments/receipts/items?from=2026-10-01&to=2026-10-31&column={}"
    payments_service.invalidate_summary()
    statements, r = capture_queries(w, lambda: own.get(url.format("received")), rows=True)
    assert r.status_code == 200, r.text
    items = r.json()
    read = sum(n for s, n in statements if "FROM student_payments" in s and "student_payments.id" in s and " ".join(s.split()).startswith("SELECT"))
    print(f"[wca] MN-16 receipts items: payment rows read by the loader and the list: {read} (18 stored, 2 in the period)")
    assert items["total"] == 2 and read <= 2 + 4                      # the list (2) + the ledgers of those 2 memberships (4), not all 16 payments of the centre
    report = own.get("/api/v1/auth/org/payments/receipts?from=2026-10-01&to=2026-10-31").json()          # the unchanged full-centre path: the oracle
    for column, key in (("received", "received"), ("to_past_debt", "to_past_debt"), ("to_current", "to_current"), ("to_advance", "to_advance")):
        listed = own.get(url.format(column)).json()
        assert sum(i["column_amount"] for i in listed["items"]) == report["totals"][key], column


# ---- MN-39: the payroll what-if of a price runs before the lock of the group ---------------------------------------------------------------------------


def test_mn39_the_payroll_what_if_runs_before_the_group_lock_and_the_answer_is_the_same(world):
    from tests.test_group_price_pg import put_price, saved_flat_month

    m, saved = saved_flat_month(world)
    dry = put_price(world, 450_000, valid_from=m.d(11), dry=True)["impact"]
    statements, out = capture_queries(world, lambda: put_price(world, 450_000, valid_from=m.d(11)))
    flat = [" ".join(s.split()) for s in statements]
    lock = next(i for i, s in enumerate(flat) if "FROM groups" in s and "FOR NO KEY UPDATE" in s)
    sheets = [i for i, s in enumerate(flat) if "FROM payroll_sheets" in s]
    print(f"[wca] MN-39 price backdated: {len(flat)} SQL, the lock is statement {lock}, payroll_sheets statements at {sheets}")
    assert sheets and max(sheets) < lock                              # nothing of the payroll what-if is read while the lock is held
    assert out["impact"]["stale_payroll_sheets"] == dry["stale_payroll_sheets"] == 1                    # the same answer as the dry run (which takes no lock at all)
    assert out["impact"]["stale_sheets"] == dry["stale_sheets"] and out["impact"]["students_following"] == dry["students_following"] == 3


# ---- LD-12(a): the settings of the centre on a payments route are the row the live-access check has read -----------------------------------------------


def test_ld12_payments_switch_reads_the_centre_row_the_dependency_has_read(pg_session):
    from sqlalchemy import event

    from src.auth.dependencies import payments_enabled_for_admin
    from src.models.auth import Organization
    from tests.test_live_access_revocation import _live_world

    w = _live_world(pg_session)
    org = pg_session.get(Organization, w.org.id)
    org.settings = {"payments": {"admin_can_manage": True}}
    pg_session.commit()
    del org
    pg_session.expire_all()
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "FROM organizations" in statement:
            seen.append(statement)

    engine = pg_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        w.resolve()                                                         # the live-access dependency
        before_switch = len(seen)
        enabled = payments_enabled_for_admin(pg_session, str(w.org.id))     # the payments switch: the row is already there
    finally:
        event.remove(engine, "before_cursor_execute", record)
    print(f"[wca] LD-12 centre reads on a payments route: dependency {before_switch}, switch {len(seen) - before_switch}")
    assert enabled is True and len(seen) == before_switch
    assert payments_enabled_for_admin(pg_session, str(uuid4())) is False and payments_enabled_for_admin(pg_session, "not-a-uuid") is False
    pg_session.get(Organization, w.org.id).settings = {"payments": {"admin_can_manage": "yes"}}
    assert payments_enabled_for_admin(pg_session, str(w.org.id)) is False        # exactly ``true`` only


# ---- LD-16: the in-memory rate limit (Redis down) has a lock and a ceiling ---------------------------------------------------------------------------


def test_ld16_memory_rate_limit_is_exact_under_threads_and_does_not_grow_without_end(monkeypatch):
    import threading
    import time

    from src.api import rate_limit

    monkeypatch.setattr(rate_limit, "_memory_store", rate_limit.defaultdict(list))
    allowed = []

    def hammer():
        for _ in range(50):
            allowed.append(rate_limit._check_memory("ip:one", 100, 60))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert allowed.count(True) == 100 and allowed.count(False) == 300       # 400 calls, a limit of 100: exactly 100 pass, whatever the interleaving
    old = time.time() - rate_limit._MEMORY_LONGEST_WINDOW - 10
    for i in range(rate_limit._MEMORY_STORE_SOFT_LIMIT + 50):
        rate_limit._memory_store[f"flood:{i}"] = [old]                       # a flood of keys, none seen for more than a day
    assert rate_limit._check_memory("ip:two", 5, 60) is True
    assert len(rate_limit._memory_store) < 100                               # swept; the live keys stay
    assert "ip:one" in rate_limit._memory_store and "ip:two" in rate_limit._memory_store


# ---- _cost_copies: one query for all the groups with a cancelled copy of a move -------------------------------------------------------------------------


def test_cost_copies_of_all_the_groups_in_one_query_with_the_same_cancels(pg_session, clock, monkeypatch):
    from src.auth import ledger_service as ls, payment_status as ps
    from src.models.auth import GroupAttendance, GroupScheduleOneOffLesson

    w = build_org(pg_session)
    gs, ss, ms = money_centre(w, clock, groups=6, recalc_history=[{"from": "2026-09-01", "on": True}])      # «вычитать уроки, отменённые центром» is on: the loader reads the cancellations
    copies = {}
    for i, g in enumerate(gs):
        copy_id = f"wc-copy-{i}-{uuid4().hex[:8]}"
        copies[g.id] = copy_id
        w.db.add(GroupScheduleOneOffLesson(id=copy_id, org_id=w.org.id, group_id=g.id, lesson_date="2026-10-07", payload={"dayOfWeek": 3, "startTime": "10:00", "endTime": "11:00"},
                                           created_by=w.owner.id, purpose="extra" if i == 0 else "moved", money="none" if i == 0 else None))      # group 0: the copy of an ADDITIONAL lesson - it never cost the month
        w.db.add(GroupAttendance(org_id=w.org.id, group_id=g.id, lesson_date="2026-10-07", lesson_id=copy_id, status="cancelled", cancellation_kind="centre", records={}, roster_snapshot=[]))
    w.db.commit()
    monkeypatch.setattr(ls.LedgerLoader, "_moved_copies", lambda self, group: {(str(group.id), copies[group.id])})       # the requests of the moves are not under test here
    loader = ls.LedgerLoader(w.db, str(w.org.id), ps.business_today())
    statements, _ = capture_queries(w, lambda: loader.load(scope="all"))
    costing = [s for s in statements if "FROM group_schedule_one_off_lessons" in s and "purpose" in s.split("WHERE")[-1]]      # the query of the cost of the copies (purpose = 'moved')
    print(f"[wca] _cost_copies: 6 groups with a cancelled copy: {len(costing)} query for the cost of the copies")
    assert len(costing) == 1
    kept = {gid: [c.lesson_id for c in loader.cancels[gid]] for gid in copies}
    assert kept[gs[0].id] == [] and all(kept[g.id] == [copies[g.id]] for g in gs[1:])       # the additional lesson's copy costs nothing; the five moved copies do


# ---- DM-011: the counters of the openings page are asked once, dropped by every write -------------------------------------------------------------------


def test_dm011_openings_summary_is_cached_and_every_write_drops_it(pg_session, clock):
    from src.auth import payments_service
    from src.models.auth import MembershipOpeningBalance

    w = build_org(pg_session)
    gs, ss, ms = money_centre(w, clock, groups=5, accounting_start_date="2026-10-01")         # they joined on 01.09: before the start, so an opening is expected from each
    payments_service.invalidate_summary()
    own = pay_client(w, "owner")
    url = "/api/v1/auth/org/opening-balances/summary"
    first_n, first = count_queries(w, lambda: own.get(url))
    second_n, second = count_queries(w, lambda: own.get(url))
    print(f"[wca] DM-011 openings summary: first {first_n} SQL, repeated {second_n} SQL")
    assert first.status_code == second.status_code == 200 and second.json() == first.json()
    assert first.json()["totals"]["memberships"] == 5 and first.json()["totals"]["missing"] == 5 and first.json()["totals"]["with_opening"] == 0
    assert first_n - second_n >= 10                                          # the table (members, periods, money, prices) is not read again
    w.db.add(MembershipOpeningBalance(org_id=w.org.id, membership_id=ms[0].id, student_id=ms[0].student_id, group_id=ms[0].group_id, paid_until=_date(2026, 10, 15),
                                      created_by=w.owner.id, source="manual"))
    w.db.commit()                                                           # a write of an opening: the counters change AT ONCE (the hook), not after the TTL
    fresh = own.get(url).json()["totals"]
    assert (fresh["with_opening"], fresh["missing"]) == (1, 4)
    assert own.post("/api/v1/auth/org/opening-balances/state", json={"state": "done"}).json()["state"] == "done"       # the state lives in the settings of the centre: dropped as well
    assert own.get(url).json()["state"] == "done"
