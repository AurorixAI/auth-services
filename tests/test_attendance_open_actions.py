"""N13 P1.1: open lessons carry the teacher; the excuses summary counts them (PostgreSQL)."""
from src.auth.schemas import GroupUpdateRequest
from src.auth.service import AuthService

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, excused_lesson, make_client, needs_pg, pg_session, reconcile, world,
)

pytestmark = needs_pg

OPEN = "/api/v1/auth/org/attendance/open-actions"
SUMMARY = "/api/v1/auth/org/attendance/excuses/summary"


def find(items, day, lesson_id, action_type):
    found = [
        i for i in items
        if i["lessonDate"] == day.isoformat() and i["lessonId"] == lesson_id and i["actionType"] == action_type
    ]
    assert len(found) == 1, (day, lesson_id, action_type, len(found))
    return found[0]


def change_group_teacher(w, teacher):
    w.svc.update_group(
        str(w.org.id), str(w.group.id), GroupUpdateRequest(teacher_id=str(teacher.id)),
        actor_id=str(w.admin.id), actor_role="admin",
    )


def test_teacher_name_present_for_both_action_types(world):
    day, lesson_id = reconcile(world, offset_days=1)
    items = make_client(world).get(OPEN).json()
    started = find(items, day, lesson_id, "in_progress")
    assert started["teacherId"] == str(world.teacher.id)
    assert started["teacherName"] == "Пётр Teacher"
    missed_day = day - (business_today() - day) * 2  # 3 days ago, never started
    missed = find(items, missed_day, f"s{missed_day.isoweekday()}", "missed")
    assert missed["teacherId"] == str(world.teacher.id)
    assert missed["teacherName"] == "Пётр Teacher"
    assert all(i["teacherName"] for i in items)


def test_in_progress_keeps_snapshot_teacher_missed_follows_group_teacher(world):
    day, lesson_id = reconcile(world, offset_days=1)
    change_group_teacher(world, world.teacher2)
    items = make_client(world).get(OPEN).json()
    started = find(items, day, lesson_id, "in_progress")
    assert started["teacherId"] == str(world.teacher.id)
    assert started["teacherName"] == "Пётр Teacher"
    missed_day = day - (business_today() - day) * 2  # 3 days ago, never started
    missed = find(items, missed_day, f"s{missed_day.isoweekday()}", "missed")
    assert missed["teacherId"] == str(world.teacher2.id)
    assert missed["teacherName"] == "Мария Teacher"


def test_in_progress_without_snapshot_falls_back_to_group_teacher(world):
    from src.models.auth import GroupAttendance

    day, lesson_id = reconcile(world, offset_days=1)
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id).update({"teacher_id": None})
    world.db.commit()
    change_group_teacher(world, world.teacher2)
    started = find(make_client(world).get(OPEN).json(), day, lesson_id, "in_progress")
    assert started["teacherName"] == "Мария Teacher"


def test_names_are_resolved_in_one_batched_query(world):
    from sqlalchemy import event

    reconcile(world, offset_days=1)
    reconcile(world, offset_days=2)
    user_selects = []

    def count(conn, cursor, statement, *a):
        if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement and "IN (" in statement:
            user_selects.append(statement)

    engine = world.db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        items = world.svc.get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(items) > 10 and all(i.teacherName for i in items)
    assert len(user_selects) == 1


def test_summary_returns_open_lessons_total_and_keeps_pending_total(world):
    excused_lesson(world)  # one pending excuse, one started lesson
    client = make_client(world)
    body = client.get(SUMMARY).json()
    expected = len(client.get(OPEN).json())
    assert body["pending_total"] == 1
    assert body["open_lessons_total"] == expected and expected > 1


def test_summary_open_lessons_are_org_scoped(world):
    foreign = make_client(world, user=world.foreign_admin, org="other")
    items = foreign.get(OPEN).json()
    assert items and {i["groupId"] for i in items} == {str(world.foreign_group.id)}
    assert foreign.get(SUMMARY).json() == {"pending_total": 0, "open_lessons_total": len(items)}


def test_summary_survives_open_lessons_failure(world, monkeypatch):
    excused_lesson(world)

    def boom(self, *a, **kw):
        raise RuntimeError("schedule is broken")

    monkeypatch.setattr(AuthService, "get_open_attendance_actions", boom)
    r = make_client(world).get(SUMMARY)
    assert r.status_code == 200
    # Not counted is not zero: the client keeps its last value.
    assert r.json() == {"pending_total": 1, "open_lessons_total": None}


def test_summary_survives_a_real_sql_error_in_the_open_lessons_count(world, monkeypatch):
    from sqlalchemy import text

    excused_lesson(world)

    def poisoned(self, *a, **kw):
        # A genuine database error: the PostgreSQL transaction is aborted.
        self.db.execute(text("SELECT * FROM table_that_does_not_exist_n13"))

    monkeypatch.setattr(AuthService, "get_open_attendance_actions", poisoned)
    client = make_client(world)
    r = client.get(SUMMARY)
    assert r.status_code == 200
    assert r.json() == {"pending_total": 1, "open_lessons_total": None}
    monkeypatch.undo()
    # The session was rolled back and is usable; the next summary is complete.
    again = client.get(SUMMARY).json()
    assert again["pending_total"] == 1 and again["open_lessons_total"] > 1
    assert world.db.execute(text("SELECT 1")).scalar() == 1


def _statements_for_open_actions(w):
    from sqlalchemy import event

    statements = []
    engine = w.db.get_bind()

    def record(conn, cursor, statement, *a):
        statements.append(statement)

    w.db.expire_all()
    event.listen(engine, "before_cursor_execute", record)
    try:
        items = w.svc.get_open_attendance_actions(str(w.org.id), actor_id=None, is_org_admin=True)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return items, statements


def test_open_actions_query_count_does_not_grow_with_group_count(world):
    from tests.pg_world import make_group, make_student

    def add_groups(count, start):
        for i in range(count):
            group = make_group(world.db, world.org, world.teacher, f"Доп {start + i}")
            make_student(world.db, world.org, group, f"Имя{start + i}", f"Фамилия{start + i}")
            if i % 3 == 0:
                reconcile(world, offset_days=1, group=group)  # a started lesson in every third group
        world.db.commit()

    add_groups(2, 0)  # 3 active groups
    small_items, small = _statements_for_open_actions(world)
    add_groups(27, 2)  # 30 active groups
    big_items, big = _statements_for_open_actions(world)
    assert len(big_items) > len(small_items) > 0
    assert len(big) == len(small), (len(small), len(big))
    assert len(big) <= 8


def test_preloaded_schedule_gives_the_same_occurrences(world):
    from datetime import timedelta
    from src.auth.service import AuthService

    today = business_today()
    kw = dict(date_from=today - timedelta(days=30), date_to=today)
    plain = world.svc._resolved_occurrences_for_group(world.group, **kw)
    pre = world.svc._preload_schedule_data([world.group.id], **kw)
    assert world.svc._resolved_occurrences_for_group(world.group, preloaded=pre, **kw) == plain


def test_teacher_still_gets_403_on_summary(world):
    assert make_client(world, user=world.teacher).get(SUMMARY).status_code == 403


def test_teacher_open_actions_only_own_groups(world):
    reconcile(world, offset_days=1)
    mine = make_client(world, user=world.teacher).get(OPEN).json()
    others = make_client(world, user=world.teacher2).get(OPEN).json()
    assert mine and all(i["teacherId"] == str(world.teacher.id) for i in mine)
    assert others == []


# ---- N13 P1.2: the missed-lesson window is "from the 1st of the previous month" ----


def test_missed_window_start_covers_the_previous_month():
    from datetime import date

    from src.auth.service import missed_lesson_window_start

    assert missed_lesson_window_start(date(2026, 10, 15)) == date(2026, 9, 1)
    assert missed_lesson_window_start(date(2026, 11, 1)) == date(2026, 10, 1)
    assert missed_lesson_window_start(date(2026, 10, 31)) == date(2026, 9, 1)
    assert missed_lesson_window_start(date(2027, 1, 5)) == date(2026, 12, 1)  # year boundary
    assert missed_lesson_window_start(date(2026, 3, 31)) == date(2026, 2, 1)  # short previous month


def test_open_actions_missed_lessons_start_on_the_first_of_the_previous_month(world):
    from datetime import datetime

    from src.auth.service import BUSINESS_TIMEZONE

    def missed_dates(now):
        items = world.svc.get_open_attendance_actions(
            str(world.org.id), actor_id=None, is_org_admin=True, now=now
        )
        return sorted(i.lessonDate for i in items if i.actionType == "missed")

    # 15.10 (a Thursday, after the lessons ended): the whole of September is covered.
    dates = missed_dates(datetime(2026, 10, 15, 23, 0, tzinfo=BUSINESS_TIMEZONE))
    assert dates[0] == "2026-09-01" and dates[-1] == "2026-10-15"
    assert "2026-08-31" not in dates
    # 01.11, early morning: October is covered from its 1st, the day itself has no past lesson yet.
    dates = missed_dates(datetime(2026, 11, 1, 5, 0, tzinfo=BUSINESS_TIMEZONE))
    assert dates[0] == "2026-10-01" and dates[-1] == "2026-10-31"
    assert not any(d.startswith("2026-09") for d in dates)
    # The preload stays O(1): a window of two months costs the same queries as before.
    _, statements = _statements_for_open_actions(world)
    assert len(statements) <= 8


# ---- N13 P1.5: no student in the group at the lesson start = nothing was missed ----


def _weekday_group(w, name, memberships):
    """A group with one 10:00-11:00 lesson per weekday and the given (joined_at, left_at) memberships.

    joined_at / left_at are naive UTC, like the real columns.
    """
    from datetime import datetime  # noqa: F401  (documentation of the types used)

    from tests.pg_world import make_group
    from src.models.auth import Student, StudentGroup

    group = make_group(w.db, w.org, w.teacher, name)
    group.schedule = [
        {"id": f"s{d}", "dayOfWeek": d, "startTime": "10:00", "endTime": "11:00", "format": "offline"}
        for d in range(1, 8)
    ]
    for i, (joined, left) in enumerate(memberships):
        student = Student(org_id=w.org.id, first_name=f"Уч{i}", last_name="Тест", class_level=9, group_id=group.id)
        w.db.add(student)
        w.db.flush()
        w.db.add(StudentGroup(student_id=student.id, group_id=group.id, is_active=left is None,
                              joined_at=joined, left_at=left))
    w.db.commit()
    return group


def _missed_for(w, group, now):
    items = w.svc.get_open_attendance_actions(str(w.org.id), actor_id=None, is_org_admin=True, now=now)
    return sorted(i.lessonDate for i in items if i.actionType == "missed" and i.groupId == str(group.id))


def _noon(day):
    from datetime import datetime

    from src.auth.service import BUSINESS_TIMEZONE

    return datetime(day.year, day.month, day.day, 23, 0, tzinfo=BUSINESS_TIMEZONE)


def test_missed_lesson_of_a_group_with_no_students_is_not_reported(world):
    empty = _weekday_group(world, "Пустая", [])
    assert _missed_for(world, empty, _noon(business_today())) == []
    # the group with students still reports its missed lessons
    assert _missed_for(world, world.group, _noon(business_today()))


def test_in_progress_lesson_stays_reported_after_the_roster_became_empty(world):
    from datetime import datetime, timedelta

    from src.auth.schemas import StartAttendanceRequest
    from src.models.auth import StudentGroup

    group = _weekday_group(world, "Начатая", [(datetime.now() - timedelta(days=90), None)])
    day, lesson_id = past_lesson_for_test()
    world.svc.reconcile_missed_group_attendance(
        str(world.org.id), str(group.id), StartAttendanceRequest(lessonDate=day, lessonId=lesson_id),
        reconciled_by=str(world.admin.id), is_org_admin=True,
    )
    # the only student leaves afterwards: someone started the lesson, so it stays an open task
    world.db.query(StudentGroup).filter_by(group_id=group.id).update({"left_at": datetime.utcnow()})
    world.db.commit()
    items = world.svc.get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True)
    started = [i for i in items if i.groupId == str(group.id) and i.actionType == "in_progress"]
    assert [i.lessonDate for i in started] == [day.isoformat()]


def past_lesson_for_test():
    from tests.pg_world import past_lesson

    return past_lesson(2)


def test_missed_lessons_start_with_the_first_student_who_joined(world):
    from datetime import datetime, timedelta

    today = business_today()
    joined_day = today - timedelta(days=10)
    # 11:00 Tashkent = 06:00 UTC: AFTER the 10:00 lesson of that day (05:00 UTC).
    joined = datetime(joined_day.year, joined_day.month, joined_day.day, 6, 0)
    group = _weekday_group(world, "Новая", [(joined, None)])
    dates = _missed_for(world, group, _noon(today))
    assert joined_day.isoformat() not in dates  # the lesson that day had started before the student came
    assert (joined_day - timedelta(days=1)).isoformat() not in dates
    assert (joined_day + timedelta(days=1)).isoformat() in dates
    assert dates[0] == (joined_day + timedelta(days=1)).isoformat() and dates[-1] == today.isoformat()
    # one hour earlier (09:30 Tashkent = 04:30 UTC) the same day's lesson counts
    group2 = _weekday_group(world, "Новая 2", [(datetime(joined_day.year, joined_day.month, joined_day.day, 4, 30), None)])
    assert _missed_for(world, group2, _noon(today))[0] == joined_day.isoformat()


def test_missed_lessons_stop_after_the_last_student_left(world):
    from datetime import datetime, timedelta

    today = business_today()
    left_day = today - timedelta(days=5)
    # 11:00 Tashkent = 06:00 UTC: AFTER the lesson start, so that day's lesson still had a student.
    left = datetime(left_day.year, left_day.month, left_day.day, 6, 0)
    group = _weekday_group(world, "Выбыли", [(datetime.now() - timedelta(days=90), left)])
    dates = _missed_for(world, group, _noon(today))
    assert dates[-1] == left_day.isoformat()
    assert (left_day + timedelta(days=1)).isoformat() not in dates


def test_a_second_student_keeps_the_lesson_after_the_first_one_left(world):
    from datetime import datetime, timedelta

    today = business_today()
    early = datetime.now() - timedelta(days=90)
    gone = datetime.now() - timedelta(days=20)
    group = _weekday_group(world, "Один остался", [(early, gone), (early, None)])
    assert _missed_for(world, group, _noon(today))[-1] == today.isoformat()


def test_roster_rule_matches_the_snapshot_builder(world):
    """The batched check and _attendance_roster_at agree at the boundary instants."""
    from datetime import datetime, timedelta

    from src.auth.service import lesson_start_as_naive_utc, membership_covers_instant

    day = business_today() - timedelta(days=3)
    start = lesson_start_as_naive_utc(day, 10 * 60)
    assert start == datetime(day.year, day.month, day.day, 5, 0)
    cases = [
        (start - timedelta(minutes=1), None),   # joined just before
        (start, None),                          # joined exactly at the start: counts
        (start + timedelta(minutes=1), None),   # joined just after
        (start - timedelta(days=9), start),     # left exactly at the start: no longer there
        (start - timedelta(days=9), start + timedelta(minutes=1)),
    ]
    for joined, left in cases:
        group = _weekday_group(world, "Граница", [(joined, left)])
        in_snapshot = bool(world.svc._attendance_roster_at(group.id, start))
        assert in_snapshot == membership_covers_instant(joined, left, start), (joined, left)
    assert membership_covers_instant(None, None, start)  # no joined_at: counted, like the SQL rule


def test_empty_roster_check_keeps_the_query_count_constant(world):
    from datetime import datetime, timedelta

    from tests.pg_world import make_group  # noqa: F401

    joined = datetime.now() - timedelta(days=90)

    def add(count, start):
        for i in range(count):
            _weekday_group(world, f"Состав {start + i}", [(joined, None)] if i % 2 == 0 else [])

    add(2, 0)
    small_items, small = _statements_for_open_actions(world)
    add(28, 2)
    big_items, big = _statements_for_open_actions(world)
    assert len(big_items) > len(small_items) > 0
    assert len(big) == len(small), (len(small), len(big))
    assert len(big) <= 9


# ---- N13 P1.5: a lesson nobody could attend cannot be reconciled either ------------

RECONCILE = "/api/v1/auth/org/groups/{gid}/attendance/reconcile"
CANCEL = "/api/v1/auth/org/groups/{gid}/attendance/reconcile/cancel"
NO_STUDENTS = "На дату урока в группе не было учеников — урок нечего отмечать. Отметьте «Урока не было», если нужно"


def _reconcile_request(world, group, day, lesson_id=None):
    return make_client(world).post(
        RECONCILE.format(gid=group.id),
        json={"lessonDate": day.isoformat(), "lessonId": lesson_id or f"s{day.isoweekday()}"},
    )


def test_reconcile_with_an_empty_roster_is_refused_with_400(world):
    from sqlalchemy import func

    from src.models.auth import GroupAttendance

    empty = _weekday_group(world, "Пустая", [])
    day, _ = past_lesson_for_test()
    r = _reconcile_request(world, empty, day)
    assert r.status_code == 400 and r.json()["detail"] == NO_STUDENTS
    # nothing was written
    assert world.db.query(func.count(GroupAttendance.id)).filter_by(group_id=empty.id).scalar() == 0


def test_reconcile_with_a_student_who_joined_before_the_lesson_works(world):
    from datetime import datetime, timedelta

    group = _weekday_group(world, "С учеником", [(datetime.now() - timedelta(days=90), None)])
    day, _ = past_lesson_for_test()
    r = _reconcile_request(world, group, day)
    assert r.status_code == 200, r.text
    items = world.svc.get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True)
    assert [i.actionType for i in items if i.groupId == str(group.id) and i.lessonDate == day.isoformat()] == ["in_progress"]


def test_reconcile_uses_the_same_rule_as_the_open_lessons_list(world):
    """The first student joined at 11:00 Tashkent: the 10:00 lesson that day is not listed and cannot be reconciled."""
    from datetime import datetime, timedelta

    today = business_today()
    joined_day = today - timedelta(days=10)
    group = _weekday_group(world, "Пришёл позже", [(datetime(joined_day.year, joined_day.month, joined_day.day, 6, 0), None)])
    listed = set(_missed_for(world, group, _noon(today)))
    assert joined_day.isoformat() not in listed and (joined_day + timedelta(days=1)).isoformat() in listed
    assert _reconcile_request(world, group, joined_day).status_code == 400
    assert _reconcile_request(world, group, joined_day - timedelta(days=1)).status_code == 400
    assert _reconcile_request(world, group, joined_day + timedelta(days=1)).status_code == 200
    # one hour earlier (09:30 Tashkent = 04:30 UTC) the same day's lesson counts
    early = _weekday_group(world, "Пришёл раньше", [(datetime(joined_day.year, joined_day.month, joined_day.day, 4, 30), None)])
    assert _reconcile_request(world, early, joined_day).status_code == 200


def test_cancelling_a_missed_lesson_of_an_empty_group_stays_allowed(world):
    empty = _weekday_group(world, "Пустая для отмены", [])
    day, lesson_id = past_lesson_for_test()
    r = make_client(world).post(
        CANCEL.format(gid=empty.id),
        json={"lessonDate": day.isoformat(), "lessonId": lesson_id, "cancellationReason": "Каникулы"},
    )
    assert r.status_code == 200, r.text
