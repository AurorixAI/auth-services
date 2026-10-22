"""LOGIC-B decision 7 - a teacher closes lessons that are at most 7 days old; older ones are the administrator's.  Matrix rows [D7.R1] ... [D7.R9]."""
from datetime import datetime

import pytest

from src.models.auth import GroupAttendance

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session,
)

pytestmark = needs_pg
API = "/api/v1/auth/org"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_d0(pg_session)
    world.G7 = new_group(pg_session, world.org, world.ids, "G7", "Гр-7", world.D,
                         [new_slot("lesson-mon", 1, "10:00", "11:00", "2026-10-01"), new_slot("lesson-tue", 2, "10:00", "11:00", "2026-10-01"),
                          new_slot("lesson-wed", 3, "10:00", "11:00", "2026-08-01")])
    new_student(pg_session, world.org, world.ids, "g7", "Седьмой", "Ученик", world.G7)
    pg_session.commit()
    return world


def rec(w, who, lesson, day, group=None):
    return client(w, who).post(f"{API}/groups/{(group or w.G7).id}/attendance/reconcile", json={"lessonDate": day, "lessonId": lesson})


def test_r1_monday_and_the_border_day_pass_the_day_before_is_too_old(w):
    """[D7.R1] Wed 21.10: Mon 19.10 yes, Wed 14.10 yes (7 days), Tue 13.10 -> 403 LESSON_TOO_OLD."""
    assert rec(w, "D", "lesson-mon", "2026-10-19").status_code == 200
    assert rec(w, "D", "lesson-wed", "2026-10-14").status_code == 200
    old = rec(w, "D", "lesson-tue", "2026-10-13")
    assert old.status_code == 403 and old.json()["detail"]["code"] == "LESSON_TOO_OLD"
    assert old.json()["detail"]["message"] == "Урок старше 7 дней — его закрывает администратор или владелец"


def test_r2_the_idempotent_reconcile_obeys_the_window_and_an_old_open_lesson_is_the_admins(w, clock):
    """[D7.R2] A reconcile repeated after the window closed is refused for a teacher; a lesson started long ago is completed by an admin only."""
    assert rec(w, "D", "lesson-wed", "2026-10-14").status_code == 200
    assert rec(w, "D", "lesson-wed", "2026-10-14").status_code == 200
    clock.set("2026-10-22 09:00")
    assert rec(w, "D", "lesson-wed", "2026-10-14").status_code == 403
    marks = {str(w.ids("g7")): "present"}
    body = {"lessonDate": "2026-10-14", "lessonId": "lesson-wed", "status": "completed", "records": marks}
    teacher = client(w, "D").post(f"{API}/groups/{w.G7.id}/attendance", json=body)
    assert teacher.status_code == 403 and teacher.json()["detail"]["code"] == "LESSON_TOO_OLD"
    assert client(w, "admin").post(f"{API}/groups/{w.G7.id}/attendance", json=body).status_code == 200
    assert rec(w, "admin", "lesson-wed", "2026-10-14").status_code in (200, 400)


def test_r3_an_admin_closes_anything_inside_the_unclosed_window_and_nothing_before_it(w):
    """[D7.R3] The administrator's lower bound is the first day of the previous month (01.09 on 21.10): 13.10 yes; 31.08 -> 422 LESSON_OUTSIDE_WINDOW (reconcile and cancel_missed)."""
    assert rec(w, "admin", "lesson-tue", "2026-10-13").status_code == 200
    gone = rec(w, "admin", "lesson-wed", "2026-08-26")
    assert gone.status_code == 422 and gone.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"
    cancel = client(w, "admin").post(f"{API}/groups/{w.G7.id}/attendance/reconcile/cancel",
                                     json={"lessonDate": "2026-08-26", "lessonId": "lesson-wed", "cancellationReason": "Каникулы"})
    assert cancel.status_code == 422 and cancel.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"
    ok = client(w, "admin").post(f"{API}/groups/{w.G7.id}/attendance/reconcile/cancel",
                                 json={"lessonDate": "2026-09-02", "lessonId": "lesson-wed", "cancellationReason": "Каникулы"})
    assert ok.status_code == 200


def test_r4_r6_the_window_is_the_business_day_at_the_moment_of_the_request(w, clock):
    """[D7.R4] [D7.R6] 23:59 Wed 21.10: the lesson of 14.10 is still 7 days old; 00:00:30 Thu 22.10 it is 8 days; the month border moves the unclosed window."""
    clock.set("2026-10-21 23:59")
    assert rec(w, "D", "lesson-wed", "2026-10-14").status_code == 200
    clock.set("2026-10-22 00:00:30")
    assert rec(w, "D", "lesson-wed", "2026-10-14").json()["detail"]["code"] == "LESSON_TOO_OLD"
    clock.set("2026-11-01 09:00")                                              # the window now starts on 01.10
    new = rec(w, "admin", "lesson-wed", "2026-09-30")
    assert new.status_code == 422 and new.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"


def test_r5_open_actions_tell_a_teacher_that_an_old_lesson_needs_an_admin(w, clock):
    """[D7.R5] An in_progress lesson 10 days old: ``requiresAdmin`` true for the teacher, false for the admin; ``ageDays`` counted."""
    history_row(w.db, w.org, w.G7, "lesson-wed", "2026-10-14", w.D, [], status="in_progress", start="10:00", records={})
    w.db.query(GroupAttendance).filter_by(group_id=w.G7.id, lesson_date="2026-10-14").update({"roster_snapshot": []})
    w.db.commit()
    clock.set("2026-10-24 12:00")
    mine = [a for a in client(w, "D").get(f"{API}/attendance/open-actions").json() if a["lessonDate"] == "2026-10-14" and a["actionType"] == "in_progress"]
    admin = [a for a in client(w, "admin").get(f"{API}/attendance/open-actions").json() if a["lessonDate"] == "2026-10-14" and a["actionType"] == "in_progress"]
    assert (mine[0]["requiresAdmin"], mine[0]["ageDays"], mine[0]["isSubstitution"], mine[0]["version"]) == (True, 10, False, 1)
    assert admin[0]["requiresAdmin"] is False
    missed = [a for a in client(w, "D").get(f"{API}/attendance/open-actions").json() if a["actionType"] == "missed" and a["lessonDate"] == "2026-10-13"]
    assert missed and missed[0]["requiresAdmin"] is True
    golden = fixture("x_open_action_requires_admin.json")
    assert golden["requiresAdmin"] is True and golden["ageDays"] == 8


def test_r7_an_unreadable_date_is_422(w):
    """[D7.R7]"""
    r = client(w, "D").post(f"{API}/groups/{w.G7.id}/attendance/reconcile", json={"lessonDate": "13/10/2026", "lessonId": "lesson-tue"})
    assert r.status_code == 422


def test_r8_start_is_still_today_only_and_roles(w, clock):
    """[D7.R8] Starting stays "today only" for everybody (also an admin); owner and admin use the unclosed window, a teacher the 7 days."""
    r = client(w, "admin").post(f"{API}/groups/{w.G7.id}/attendance/start", json={"lessonDate": "2026-10-19", "lessonId": "lesson-mon"})
    assert r.status_code == 400 and "в день его проведения" in r.json()["detail"]
    assert rec(w, "owner", "lesson-tue", "2026-10-13").status_code == 200
