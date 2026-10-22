"""R3 review fixes of pack B (lead, 21.10): restore overlap, rollback-safe intervals, lock order, removal vs a substitute's start, access, nits, import."""
from datetime import date, datetime

import pytest

from src.models.auth import Group

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg
G = "/api/v1/auth/org/groups/{gid}"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-22 10:00")
    return build_d0(pg_session)


def refresh(w, group):
    w.db.expire_all()
    return w.db.get(Group, group.id)


# ------------------------------------------------------------------------------------------------------------------------------ 1
def test_restore_with_the_same_teacher_checks_the_overlap(w):
    """R3-1: while the group slept its teacher got another lesson at that time: restore without a new teacher -> 409 TEACHER_BUSY."""
    slot = new_slot("lesson-f", 5, "15:00", "16:00", "2026-10-22")
    a = new_group(w.db, w.org, w.ids, "FA", "Архивная", w.C, [slot])
    w.db.commit()
    assert client(w, "admin").post(G.format(gid=a.id) + "/archive", json={}).status_code == 200
    new_group(w.db, w.org, w.ids, "FB", "Новая", w.C, [new_slot("lesson-g", 5, "15:30", "16:30", "2026-10-22")])
    w.db.commit()
    r = client(w, "admin").post(G.format(gid=a.id) + "/restore", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_BUSY", r.text
    assert refresh(w, a).status == "archived"
    # another teacher who is free restores it
    ok = client(w, "admin").post(G.format(gid=a.id) + "/restore", json={"teacherId": str(w.D.id)})
    assert ok.status_code == 200 and ok.json()["teacher_id"] == str(w.D.id)


# ------------------------------------------------------------------------------------------------------------------------------ 2
def test_open_interval_on_an_active_group_is_closed_and_restore_without_interval_uses_archived_at(w, clock):
    """R3-2: an interval left open on an ACTIVE group (rolled-back image) hides nothing after the row's last update; an archived group without an interval
    gets [archived_at, now) on restore."""
    from src.auth import schedule_rules

    g = new_group(w.db, w.org, w.ids, "FI", "Интервал", w.C, [new_slot("lesson-i", 1, "10:00", "11:00", "2026-10-12")])
    g.inactive_intervals = [{"from": "2026-10-20T05:00:00", "to": None}]
    w.db.commit()
    w.db.expire_all()
    g = w.db.get(Group, g.id)
    ivs = schedule_rules.parse_intervals(g)
    assert ivs[0][1] is not None and ivs[0][1] >= ivs[0][0]
    days = [o["occurrenceDate"] for o in w.svc._resolved_occurrences_for_group(g, date_from=date(2026, 10, 26), date_to=date(2026, 11, 2))]
    assert days == ["2026-10-26", "2026-11-02"]
    # archived by an old image: no interval at all
    h = new_group(w.db, w.org, w.ids, "FH", "Старая", w.C, [new_slot("lesson-h", 3, "18:00", "19:00", "2026-10-12")])
    h.status = "archived"
    h.inactive_intervals = []
    w.db.commit()
    clock.set("2026-11-05 14:00")
    r = client(w, "admin").post(G.format(gid=h.id) + "/restore", json={})
    assert r.status_code == 200, r.text
    h = refresh(w, h)
    assert len(h.inactive_intervals) == 1 and h.inactive_intervals[0]["to"] == "2026-11-05T09:00:00" and h.inactive_intervals[0]["from"] < "2026-11-05"


# ------------------------------------------------------------------------------------------------------------------------------ 3
def test_group_row_locks_are_taken_in_ascending_id(w):
    """R3-3: ``_teaching_groups(lock=True)`` locks in id order (the name order is only for the answer)."""
    from sqlalchemy import event

    from src.auth.staff_lifecycle import StaffLifecycle

    statements = []
    engine = w.db.get_bind()

    def spy(conn, cursor, statement, *a):
        if "FOR UPDATE" in statement and "FROM groups" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        groups = StaffLifecycle(w.svc)._teaching_groups(w.org.id, w.A.id, lock=True)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert statements and "ORDER BY groups.id" in statements[0]
    assert [g.name for g in groups] == sorted(g.name for g in groups)                      # the answer is still in name order
    w.db.rollback()


# ------------------------------------------------------------------------------------------------------------------------------ 4
def test_removal_waits_for_a_substitute_who_starts_his_lesson(w, clock):
    """R3-4: D substitutes for lesson-l1 of 9-А.  A session holds the group lock and writes D's in_progress row; remove(D) must WAIT for it and then refuse
    (MEMBER_HAS_ASSIGNMENTS with the lesson), never delete a member who leads a lesson."""
    import threading
    import time

    from sqlalchemy.orm import sessionmaker

    from src.auth.service import AuthService
    from src.models.auth import GroupAttendance

    assert client(w, "admin").put(G.format(gid=w.G1.id) + "/lesson-teacher", json={
        "lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    clock.set("2026-10-26 10:00")
    w.db.commit()
    Session = sessionmaker(w.db.get_bind())
    s2 = Session()
    s2.execute(__import__("sqlalchemy").text("SELECT id FROM groups WHERE id = :g FOR UPDATE"), {"g": w.G1.id})
    outcome = {}

    def remover():
        session = Session()
        try:
            outcome["r"] = AuthService(session).remove_member(str(w.org.id), str(w.ids("member_D")), actor_id=str(w.owner.id))
        except BaseException as exc:  # noqa: BLE001
            session.rollback()
            outcome["e"] = exc
        finally:
            session.close()

    t = threading.Thread(target=remover)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "removal must wait for the group lock of the substitute's lesson"
    s2.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.D.id, lesson_date="2026-10-26", lesson_id="lesson-l1", status="in_progress",
                           records={}, roster_snapshot=[], marked_by=w.D.id, started_at=__import__("datetime").datetime(2026, 10, 26, 5, 0), version=1))
    s2.commit()
    s2.close()
    t.join(timeout=20)
    assert not t.is_alive()
    err = outcome.get("e")
    assert err is not None and "MEMBER_HAS_ASSIGNMENTS" in str(getattr(err, "detail", err)), outcome
    w.db.expire_all()
    assert w.member("D").is_active is True


# ------------------------------------------------------------------------------------------------------------------------------ 5
B = "/api/v1/auth/org"


def _substitute(w, who="D", lesson="lesson-l1", day="2026-10-26"):
    r = client(w, "admin").put(G.format(gid=w.G1.id) + "/lesson-teacher", json={"lessonDate": day, "lessonId": lesson, "teacherId": str(getattr(w, who).id), "reason": "Болезнь"})
    assert r.status_code == 200, r.text
    w.db.commit()


def test_substitute_access_is_the_substitution_lesson_only_per_endpoint(w, clock):
    """R3-5 / B2 'права заменяющего': D substitutes for lesson-l1 of 26.10 in 9-А (teacher A).  Every endpoint of the group answers for exactly that lesson, 403 for the rest."""
    s1 = w.s1
    s1.student_phone = "+998901234567"
    w.db.commit()
    _substitute(w)
    d = client(w, "D")
    g = str(w.G1.id)
    # the group page, its statistics, the list of "my groups"
    assert d.get(f"{B}/groups/{g}").status_code == 403
    assert d.get(f"{B}/groups/{g}/stats").status_code == 403
    assert g not in [x["id"] for x in d.get(f"{B}/groups").json()]
    # the journal: only his lesson, the roster without phones and diagnostics
    j = d.get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"})
    assert j.status_code == 200, j.text
    out = j.json()
    assert [x["instanceKey"] for x in out["lessons"]] == ["2026-10-26_lesson-l1"]
    assert set(out["statuses"]) <= {"2026-10-26_lesson-l1"} and set(out["lessonTeachers"]) == {"2026-10-26_lesson-l1"}
    assert {s["display_id"] for s in out["lessonRoster"]} == {"AL09-001", "AL09-002", "AL09-003"}
    assert "998901234567" not in j.text and "theta" not in j.text and "student_phone" not in j.text
    assert out["membershipEvents"] == [] and out["diagnosticReportNotices"] == [] and out["historicalStudents"] == []
    assert d.get(f"{B}/groups/{g}/attendance", params={"month": "2026-11"}).json()["lessons"] == []
    assert d.get(f"{B}/groups/{g}/attendance", params={"month": "2026-09"}).json()["lessons"] == []                    # the history of the group is closed
    # the schedule: only his lesson
    occ = d.get(f"{B}/schedule/occurrences", params={"date_from": "2026-10-22", "date_to": "2026-11-08"}).json()
    assert [(o["id"], o["occurrenceDate"], o["isSubstitution"]) for o in occ] == [("lesson-l1", "2026-10-26", True)]
    # a lesson: start / marks / reconcile of ANOTHER lesson -> 403
    base = f"{B}/groups/{g}/attendance"
    clock.set("2026-10-28 16:05")
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    for path, body in (("/start", {"lessonDate": "2026-10-28", "lessonId": "lesson-l2"}),
                       ("", {"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "status": "in_progress", "records": marks}),
                       ("/reconcile", {"lessonDate": "2026-10-19", "lessonId": "lesson-l1"}),
                       ("/reconcile/cancel", {"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "cancellationReason": "xxx"})):
        r = d.post(base + path, json=body)
        assert r.status_code == 403, (path, r.status_code, r.text)
    clock.set("2026-10-26 10:00")
    assert d.post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200      # HIS lesson
    # students, profiles, payments, writes on the group
    assert d.get(f"{B}/students/{s1.id}").status_code in (403, 404)
    assert d.get(f"{B}/students/{s1.id}/profile").status_code in (403, 404)
    listed = d.get(f"{B}/students", params={"group_id": g})
    assert listed.status_code == 403 or listed.json() == [] or listed.json().get("items") == [], listed.text
    assert d.get(f"{B}/groups/{g}/prices").status_code == 403
    assert d.put(f"{B}/groups/{g}", json={"name": "x"}).status_code == 403
    assert d.post(f"{B}/groups/{g}/students/{w.s4.id}").status_code == 403
    assert d.post(f"{B}/groups/{g}/archive", json={}).status_code == 403
    assert d.put(G.format(gid=g) + "/lesson-teacher", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "teacherId": str(w.D.id), "reason": "x"}).status_code == 403
    # the assignment taken off: the access goes with it, at once
    clock.set("2026-10-22 11:00")
    _substitute(w, "C", "lesson-l2", "2026-10-28")
    c = client(w, "C")
    assert c.get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"}).status_code == 200
    assert client(w, "admin").put(G.format(gid=g) + "/lesson-teacher", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "teacherId": None,
                                                                              "reason": "Замена отменена"}).status_code == 200
    assert c.get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"}).status_code == 403


def test_former_teacher_after_a_handover_reads_only_his_own_past_lessons(w, clock):
    """R3-5: A hands 9-А over to C: A sees (marks only, no phones) the lessons of the last 7 days that were his, nothing of the future, nothing of the group page."""
    w.s1.student_phone = "+998901234567"
    w.db.commit()
    r = client(w, "admin").put(f"{B}/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)})
    assert r.status_code == 200, r.text
    a = client(w, "A")
    g = str(w.G1.id)
    assert a.get(f"{B}/groups/{g}").status_code == 403 and g not in [x["id"] for x in a.get(f"{B}/groups").json()]
    j = a.get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"})
    assert j.status_code == 200, j.text
    out = j.json()
    keys = [x["instanceKey"] for x in out["lessons"]]
    assert "2026-10-19_lesson-l1" in keys and "2026-10-12_lesson-l1" not in keys                    # inside / outside the 7 days
    assert all(k.split("_")[0] < "2026-10-22" for k in keys), keys                                    # no future lesson: they are C's
    assert "998901234567" not in j.text and out["lessonRoster"] and all("phone" not in x for x in out["lessonRoster"])
    # the new teacher sees the whole journal
    full = client(w, "C").get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"}).json()
    assert len(full["lessons"]) > len(out["lessons"]) and full.get("lessonRoster") in (None, [])


# ------------------------------------------------------------------------------------------------------------------------------ 6
def test_nits_email_invalid_catalogue_lesson_date_422_and_blocker_reason(w, exams):
    from src.auth import ops_rules
    from src.auth.group_lifecycle import _blocker_reason

    assert ops_rules.ERROR_CATALOGUE["EMAIL_INVALID"][0] == 422
    r = client(w, "owner").post(f"{B}/invitations", json={"email": "not-an-email", "org_role": "teacher"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "EMAIL_INVALID"
    bad = client(w, "admin").get(G.format(gid=w.G1.id) + "/lesson-teacher-candidates", params={"lessonId": "lesson-l1", "lessonDate": "2026-13-45"})
    assert bad.status_code == 422, bad.text
    assert _blocker_reason({"status": "active"}) == "Экзамен уже идёт"
    assert _blocker_reason({"status": "lobby"}) == "У учеников уже есть попытки"
    assert _blocker_reason({"status": "active", "blockerReason": "X"}) == "X"


# ------------------------------------------------------------------------------------------------------------------------------ 7
def test_missed_lesson_of_a_group_archived_after_its_start_is_listed_and_resolvable(w, clock):
    """R3-7 / spec 5.2: archived 10:05, the lesson 10:00-11:30 was never started -> after 11:30 it is in the open actions (the admin resolves it); the lesson of the next
    week, inside the archive, is not there."""
    from src.auth.membership_periods import close_period
    from src.models.auth import StudentGroup

    g = new_group(w.db, w.org, w.ids, "FM", "Закрытая", w.C, [new_slot("lesson-m", 4, "10:00", "11:30", "2026-10-22")])
    st = new_student(w.db, w.org, w.ids, "fm1", "Бывший", "Ученик", g)
    close_period(w.db.query(StudentGroup).filter_by(student_id=st.id).one(), datetime(2026, 10, 22, 5, 2))      # left at 10:02 Tashkent, after the lesson began
    w.db.commit()
    clock.set("2026-10-22 10:05")
    assert client(w, "admin").post(G.format(gid=g.id) + "/archive", json={}).status_code == 200
    clock.set("2026-10-22 11:40")
    items = [a for a in client(w, "admin").get(f"{B}/attendance/open-actions").json() if a["groupId"] == str(g.id)]
    assert [(a["lessonId"], a["lessonDate"], a["actionType"]) for a in items] == [("lesson-m", "2026-10-22", "missed")]
    clock.set("2026-10-29 12:00")                                                         # the next week's lesson began inside the archive interval: not an occurrence
    items = [a for a in client(w, "admin").get(f"{B}/attendance/open-actions").json() if a["groupId"] == str(g.id)]
    assert [a["lessonDate"] for a in items] == ["2026-10-22"]
    r = client(w, "admin").post(f"{B}/groups/{g.id}/attendance/reconcile/cancel", json={"lessonDate": "2026-10-22", "lessonId": "lesson-m", "cancellationReason": "Группа закрыта"})
    assert r.status_code == 200, r.text
    assert [a for a in client(w, "admin").get(f"{B}/attendance/open-actions").json() if a["groupId"] == str(g.id)] == []


# ------------------------------------------------------------------------------------------------------------------------------ 8
def test_import_row_errors_are_skipped_rows_never_a_500(w, monkeypatch):
    """R3-8: a ValueError / a lock timeout / any exception of one row ends in ``skipped`` with a reason; the other rows of the file are enrolled."""
    from sqlalchemy.exc import OperationalError

    from src.auth.service import AuthService
    from tests.test_ops_o9_import_pg import book, post

    real = AuthService.add_student_to_group

    def flaky(self, org_id, group_id, student_id, **kw):
        name = self.db.get(__import__("src.models.auth", fromlist=["Student"]).Student, student_id).last_name
        if name == "Странный":
            raise ValueError("что-то совсем непонятное")
        if name == "Занятый":
            raise OperationalError("UPDATE", {}, Exception("lock timeout"))
        if name == "Взрывной":
            raise RuntimeError("boom")
        return real(self, org_id, group_id, student_id, **kw)

    monkeypatch.setattr(AuthService, "add_student_to_group", flaky)
    data = book([["9-А", 9, None, None]], [["Странный", "Али", 9, None, "9-А", None], ["Занятый", "Бек", 9, None, "9-А", None],
                                              ["Взрывной", "Тим", 9, None, "9-А", None]])
    w.db.query(__import__("src.models.auth", fromlist=["StudentGroup"]).StudentGroup).filter_by(group_id=w.G1.id).delete()      # room in 9-А
    w.db.commit()
    r = post(w, "commit", data)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["counts"]["students_enrolled"] == 0 and out["counts"]["students_skipped"] == 3
    assert sorted(s["code"] for s in out["skipped"]) == ["ROW_ERROR", "ROW_ERROR", "ROW_LOCKED"]


# ------------------------------------------------------------------------------------------------------------------------------ 9
def test_busy_counts_a_lesson_frozen_on_the_old_teacher_after_a_handover(w, clock):
    """R3-9: A started 9-А lesson-l2 (Wed 21.10 16:00), the group went to C: the running lesson is still A's - A is busy at that time (and C is not)."""
    clock.set("2026-10-21 16:20")
    base = f"{B}/groups/{w.G1.id}/attendance"
    assert client(w, "A").post(base + "/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    assert client(w, "admin").put(f"{B}/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)}).status_code == 200
    from src.auth.lesson_teachers import LessonTeachers

    items = LessonTeachers(w.svc).busy_items(w.org.id, w.A.id, date(2026, 10, 21), date(2026, 10, 21))
    assert [(i.group_name, i.startTime) for i in items] == [("9-А", "16:00")]
    c_items = LessonTeachers(w.svc).busy_items(w.org.id, w.C.id, date(2026, 10, 21), date(2026, 10, 21))
    assert [i.group_name for i in c_items] == []


def test_candidates_list_has_a_constant_number_of_queries(w):
    """R3-9: the candidates answer is built from ONE pass over the groups, the number of statements does not grow with the number of teachers."""
    from sqlalchemy import event

    counts = []
    engine = w.db.get_bind()
    n = {"v": 0}

    def spy(conn, cursor, statement, *a):
        n["v"] += 1

    def measure():
        n["v"] = 0
        event.listen(engine, "before_cursor_execute", spy)
        try:
            r = client(w, "admin").get(G.format(gid=w.G1.id) + "/lesson-teacher-candidates", params={"lessonId": "lesson-l1", "lessonDate": "2026-10-26"})
        finally:
            event.remove(engine, "before_cursor_execute", spy)
        assert r.status_code == 200, r.text
        return n["v"], len(r.json()["candidates"])

    counts.append(measure())
    from src.models.auth import OrganizationMember, User

    for i in range(6):
        u = User(id=w.ids(f"X{i}"), email=f"x{i}-{w.salt}@algo.test", password_hash="x", first_name="Иван", last_name=f"Новый{i}", role="teacher")
        w.db.add(u)
        w.db.flush()
        w.db.add(OrganizationMember(org_id=w.org.id, user_id=u.id, org_role="teacher", is_active=True))
    w.db.commit()
    counts.append(measure())
    assert counts[1][1] == counts[0][1] + 6
    assert counts[1][0] <= counts[0][0] + 2, counts


def test_cancelling_a_never_started_old_lesson_is_outside_the_window(w, clock):
    """R3-9: the cancellation through the save path (no row yet) obeys the unclosed-lessons window like reconcile."""
    clock.set("2026-10-21 14:00")
    old = new_group(w.db, w.org, w.ids, "FO", "Старая", w.C, [new_slot("lesson-o", 1, "10:00", "11:00", "2026-08-03")])
    w.db.commit()
    r = client(w, "admin").post(f"{B}/groups/{old.id}/attendance", json={"lessonDate": "2026-08-31", "lessonId": "lesson-o", "status": "cancelled",
                                                                          "cancellationReason": "Старый урок", "records": {}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW", r.text


# ------------------------------------------------------------------------------------------------------------------------------ exam release sweep
def test_exam_release_sweep_is_idempotent(w, exams):
    """INV-O4-4 (lead decision 21.10): local truth first; the sweep releases once and a second run neither asks exam-service nor changes anything."""
    from src.auth.group_lifecycle import GroupLifecycle

    g = new_group(w.db, w.org, w.ids, "FS", "Метка", w.C, [])
    g.status, g.exam_release_pending = "archived", True
    w.db.commit()
    exams.group_exams = [{"examId": "EX_9", "title": "К", "status": "lobby", "lessonDate": "2026-10-29", "canRelease": True, "blockerReason": None}]
    life = GroupLifecycle(w.svc)
    assert life.retry_pending_releases() == 1
    releases = [c for c in exams.calls if c[0].endswith("/archive-release")]
    assert len(releases) == 1 and refresh(w, g).exam_release_pending is False
    assert life.retry_pending_releases() == 0
    assert len([c for c in exams.calls if c[0].endswith("/archive-release")]) == 1            # nothing was asked again
    assert refresh(w, g).status == "archived"
