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
