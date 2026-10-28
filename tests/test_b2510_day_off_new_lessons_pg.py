"""Пакет 25.10: a lesson that appears AFTER a «Выходной день центра» does not stay on the holiday (PostgreSQL).

A day off cancels the lessons that EXIST when it is created.  A weekly lesson created later, or a series moved by a revision onto the day, gets its occurrence on the
day off cancelled by the SAME step the day off uses for its own lessons (``DaysOffService.apply_to_lesson`` -> ``_run``: the usual cancellation, kind ``holiday``,
``day_off_id``, «Выходной (…)»).  A one-off lesson and a move onto the date are deliberate (a make-up): a warning, never a block."""
import pytest

from src.auth.service import AuthService
from src.models.auth import GroupAttendance, GroupAttendanceAudit, OrgDayOff

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, build_org, centre, client, rows, statement
from tests.test_payf_recalc_pg import month_world

pytestmark = needs_pg
DAY_OFF = {"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}     # a Wednesday
NEW_WED = {"dayOfWeek": 3, "startTime": "15:00", "endTime": "16:00", "validFrom": "2026-10-20"}
SENTENCE = "28.10 — выходной день центра (Хайит): урок в этот день отменён автоматически"


@pytest.fixture
def world(pg_session, clock):
    clock.set("2026-10-19 12:00")                                       # a Monday
    w = build_org(pg_session)
    (g,), (s,), _ = centre(w)
    return w, g, s


def day_off(w, body=DAY_OFF):
    r = client(w, "owner").post(f"{API}/org/days-off", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def new_weekly(w, g, who="owner", **over):
    r = client(w, who).post(f"{API}/org/schedule/lessons", json={"groupId": str(g.id), **NEW_WED, **over})
    assert r.status_code == 201, r.text
    return r.json()


def att(w, lesson_id=None, day=None):
    w.db.expire_all()
    q = w.db.query(GroupAttendance).filter_by(org_id=w.org.id)
    if lesson_id:
        q = q.filter_by(lesson_id=lesson_id)
    if day:
        q = q.filter_by(lesson_date=day)
    return q.order_by(GroupAttendance.lesson_date, GroupAttendance.lesson_id).all()


def test_a_new_weekly_lesson_on_a_day_off_is_cancelled_as_a_holiday_and_only_that_day(world):
    w, g, _ = world
    out = day_off(w)
    assert [l["lessonId"] for l in out["cancelled"]] == ["lesson-wed"]
    new = new_weekly(w, g)
    assert [(x["code"], x["message"]) for x in new["warnings"]] == [("DAY_OFF_CANCELLED", SENTENCE)]
    (row,) = att(w, new["id"])
    assert (row.lesson_date, row.status, row.cancellation_kind, row.cancellation_reason, str(row.day_off_id), row.version) == \
        ("2026-10-28", "cancelled", "holiday", "Выходной (Хайит)", out["dayOff"]["id"], 1)
    # the usual cancellation wrote its own journal row (one road), the other Wednesdays of the series are untouched
    assert w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id, action="cancelled").count() == 1
    assert [a.lesson_date for a in att(w, "lesson-wed")] == ["2026-10-28"]               # the old lesson: still its one row, not touched twice


def test_a_teacher_who_adds_the_lesson_gets_the_same_result(world):
    w, g, _ = world
    out = day_off(w)
    new = new_weekly(w, g, who="A")                                      # the teacher of the group: no right to cancel a lesson as a holiday by hand, the day off does it
    assert [x["code"] for x in new["warnings"]] == ["DAY_OFF_CANCELLED"]
    (row,) = att(w, new["id"])
    assert (row.cancellation_kind, str(row.day_off_id)) == ("holiday", out["dayOff"]["id"])


def test_a_series_that_starts_after_the_day_off_or_a_voided_day_off_is_not_touched(world):
    w, g, _ = world
    out = day_off(w)
    late = new_weekly(w, g, validFrom="2026-10-29", startTime="16:00", endTime="17:00")          # Wednesdays from 04.11: the day off is behind
    assert late["warnings"] == [] and att(w, late["id"]) == []
    assert client(w, "owner").post(f"{API}/org/days-off/{out['dayOff']['id']}/void").status_code == 200
    again = new_weekly(w, g, startTime="17:00", endTime="18:00")
    assert again["warnings"] == [] and att(w, again["id"]) == []             # a voided day off is no day off


def test_a_revision_of_a_series_onto_a_day_off_cancels_it(world):
    w, g, _ = world
    out = day_off(w)
    r = client(w, "owner").put(f"{API}/org/schedule/lessons/lesson-fri/recurrence", json={
        "effectiveFrom": "2026-10-26", "dayOfWeek": 3, "startTime": "12:00", "endTime": "13:00", "reason": "Перенос серии"})
    assert r.status_code == 200, r.text
    assert [(x["code"], x["message"]) for x in r.json()["warnings"]] == [("DAY_OFF_CANCELLED", SENTENCE)]
    (row,) = att(w, "lesson-fri")
    assert (row.lesson_date, row.status, row.cancellation_kind, str(row.day_off_id)) == ("2026-10-28", "cancelled", "holiday", out["dayOff"]["id"])
    # a revision that changes only the room keeps the cancelled lesson as it is: no second row, no warning
    r2 = client(w, "owner").put(f"{API}/org/schedule/lessons/lesson-fri/recurrence", json={"effectiveFrom": "2026-11-02", "room": "Кабинет 7", "reason": "Другой кабинет"})
    assert r2.status_code == 200, r2.text
    assert [x["code"] for x in r2.json()["warnings"] if x["code"].startswith("DAY_OFF")] == [] and len(att(w, "lesson-fri")) == 1


def test_a_one_off_lesson_on_a_day_off_is_not_cancelled_only_a_warning(world):
    w, g, _ = world
    day_off(w)
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-10-28", "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert [x["code"] for x in body["warnings"]] == ["DAY_OFF"] and "(Хайит)" in body["warnings"][0]["message"] and "28.10" in body["warnings"][0]["message"]
    assert att(w, body["id"]) == []                                              # a deliberate lesson (a make-up): alive
    quiet = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-10-29", "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
    assert quiet.json()["warnings"] == []


def test_a_move_onto_a_day_off_is_a_warning_in_the_check_and_in_the_answer(world):
    w, g, _ = world
    day_off(w)
    q = {"lessonId": "lesson-mon", "lessonDate": "2026-10-26", "newDate": "2026-10-28", "startTime": "15:00", "endTime": "16:00"}
    check = client(w, "admin").get(f"{API}/org/groups/{g.id}/lesson-move-check", params=q).json()
    assert check["ok"] is True and [x["code"] for x in check["warnings"]] == ["DAY_OFF"] and "(Хайит)" in check["warnings"][0]["message"]
    free = client(w, "admin").get(f"{API}/org/groups/{g.id}/lesson-move-check", params={**q, "newDate": "2026-10-29"}).json()
    assert free["ok"] is True and free["warnings"] == []
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={
        "lessonId": "lesson-mon", "lessonDate": "2026-10-26", "newDate": "2026-10-28", "startTime": "15:00", "endTime": "16:00", "reason": "Перенос по просьбе центра"})
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["result"]["warnings"]] == ["DAY_OFF"] and "(Хайит)" in r.json()["result"]["warnings"][0]["message"]
    assert [a.lesson_id for a in att(w, day="2026-10-28") if a.status == "cancelled" and a.cancellation_kind == "holiday"] == ["lesson-wed"]      # the moved lesson lives


def test_voiding_the_day_off_gives_back_the_new_lessons_too(world):
    w, g, _ = world
    out = day_off(w)
    new = new_weekly(w, g)
    revised = client(w, "owner").put(f"{API}/org/schedule/lessons/lesson-fri/recurrence", json={
        "effectiveFrom": "2026-10-26", "dayOfWeek": 3, "startTime": "12:00", "endTime": "13:00", "reason": "Перенос серии"})
    assert revised.status_code == 200 and len(att(w, day="2026-10-28")) == 3
    voided = client(w, "owner").post(f"{API}/org/days-off/{out['dayOff']['id']}/void")
    assert voided.status_code == 200, voided.text
    body = voided.json()
    assert sorted(x["lessonId"] for x in body["restored"]) == sorted(["lesson-wed", new["id"], "lesson-fri"]) and body["kept"] == []
    assert att(w, day="2026-10-28") == []
    w.db.expire_all()
    assert w.db.query(OrgDayOff).filter_by(id=out["dayOff"]["id"]).one().voided_at is not None
    plan = client(w, "owner").post(f"{API}/org/days-off/preview", json=DAY_OFF).json()                # the lessons are alive again: the same day off would cancel them again
    assert len(plan["cancellable"]) == 3


def test_a_lesson_that_cannot_be_cancelled_is_a_warning_and_apply_finishes_it(world, monkeypatch):
    w, g, _ = world
    out = day_off(w)
    state = {"down": True}

    def check(self, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
        if state["down"] and phase == "cancellation-check" and lesson_id.startswith("lesson-") and lesson_id != "lesson-wed":
            raise ValueError("У урока идёт экзамен")

    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", check)
    new = new_weekly(w, g)
    assert [x["code"] for x in new["warnings"]] == ["DAY_OFF_NOT_CANCELLED"] and "У урока идёт экзамен" in new["warnings"][0]["message"] and "вручную" in new["warnings"][0]["message"]
    assert att(w, new["id"]) == []                                                  # the lesson is created, the day off left it alone
    state["down"] = False
    done = client(w, "owner").post(f"{API}/org/days-off/{out['dayOff']['id']}/apply").json()
    assert [l["lessonId"] for l in done["cancelled"]] == [new["id"]] and done["complete"] is True
    assert len(att(w, new["id"])) == 1


def test_money_a_holiday_cancellation_of_a_new_lesson_is_not_recalculated(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)                                                 # «свой месяц» + the setting «пересчитывать отмены центра» is ON; today 25.10
    before = (rows(w, s)[0]["debt"], statement(w, m)["summary"]["recalculated"])
    out = day_off(w)
    new = new_weekly(w, g, validFrom="2026-10-26")
    assert [x["code"] for x in new["warnings"]] == ["DAY_OFF_CANCELLED"]
    (row,) = att(w, new["id"])
    assert row.cancellation_kind == "holiday"
    assert (rows(w, s)[0]["debt"], statement(w, m)["summary"]["recalculated"]) == before == (600_000, 0)
    assert not [r for r in statement(w, m)["rows"] if r["type"] == "recalc"]
