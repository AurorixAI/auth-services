"""PAY-F parts 1 and 2 on PostgreSQL: the forecast «хватит до» of a package (scenarios 1-3) and «Выходной день центра» (scenarios 4-6).

Spec: ``artifacts/payf/PAY_F_SPEC.md``.  The day off cancels every lesson through THE usual cancellation (``AuthService.save_group_attendance``); the exam-service
check is the one the cancellation makes (here a stub that refuses one lesson)."""
from datetime import datetime

import pytest

from src.auth.service import AuthService
from src.models.auth import GroupAttendance, GroupAttendanceAudit, OrgDayOff

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, build_org, cancel, centre, client, pause, pay, rows

pytestmark = needs_pg
LESSONS = dict(period_mode="lessons", lessons_per_payment=12)


def package_world(w, clock, day="2026-10-05"):
    """Mon / Wed / Fri, a package of 12, the price 600 000; the student joined in September, paid on 05.10, nothing held yet."""
    clock.set(f"{day} 12:00")
    (g,), (s,), (m,) = centre(w, joined=datetime(2026, 9, 1, 6, 0), **LESSONS)
    pay(w, m, 600_000, D(2026, 10, 5))
    return g, s, m


def row_of(w, s):
    (row,) = rows(w, s)
    return row


# ---- Part 1: the forecast ---------------------------------------------------------------------------------------------------------------------------------


def test_1_package_of_12_on_mon_wed_fri_lasts_until_30_10(pg_session, clock):
    w = build_org(pg_session)
    _, s, _ = package_world(w, clock)
    row = row_of(w, s)
    assert (row["period_mode"], row["lessons_left"], row["lessons_total"]) == ("lessons", 12, 12)
    assert row["paid_through_forecast"] == "2026-10-30"          # 5, 7, 9, 12, 14, 16, 19, 21, 23, 26, 28, 30


def test_2_a_day_off_on_21_10_moves_the_forecast_to_02_11(pg_session, clock):
    w = build_org(pg_session)
    g, s, _ = package_world(w, clock)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-21", "dateTo": "2026-10-21", "reason": "Хайит"})
    assert r.status_code == 201 and [l["date"] for l in r.json()["cancelled"]] == ["2026-10-21"]
    assert row_of(w, s)["paid_through_forecast"] == "2026-11-02"


def test_2_a_cancelled_lesson_of_any_kind_and_a_moved_lesson_shift_the_forecast_by_their_dates(pg_session, clock):
    w = build_org(pg_session)
    g, s, _ = package_world(w, clock)
    cancel(w, g, "lesson-fri", "2026-10-09", kind="centre")                    # a lesson of the centre's fault
    assert row_of(w, s)["paid_through_forecast"] == "2026-11-02"
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={
        "lessonId": "lesson-mon", "lessonDate": "2026-10-12", "newDate": "2026-10-13", "startTime": "10:00", "endTime": "11:00", "reason": "Перенос по просьбе центра"})
    assert r.status_code == 200, r.text
    # 5, 7, [9 cancelled], [12 moved] 13, 14, 16, 19, 21, 23, 26, 28, 30, 02.11 -> the 12th lesson
    assert row_of(w, s)["paid_through_forecast"] == "2026-11-02"


def test_3_a_pause_of_the_student_is_skipped(pg_session, clock):
    w = build_org(pg_session)
    _, s, m = package_world(w, clock)
    pause(w, m, D(2026, 10, 12), D(2026, 10, 16))
    row = row_of(w, s)
    assert row["paid_through_forecast"] == "2026-11-06"          # 5, 7, 9, [12, 14, 16 pause], 19, 21, 23, 26, 28, 30, 02.11, 04.11, 06.11


def test_1_no_forecast_when_the_package_is_over_or_in_a_month_mode(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-10-05 12:00")
    (g,), (s,), (m,) = centre(w, joined=datetime(2026, 9, 1, 6, 0), **LESSONS)
    assert row_of(w, s)["paid_through_forecast"] is None        # nothing paid: nothing left
    pay(w, m, 600_000, D(2026, 10, 5))
    assert row_of(w, s)["paid_through_forecast"] == "2026-10-30"
    w2 = build_org(pg_session)
    clock.set("2026-10-21 12:00")
    (_,), (s2,), (m2,) = centre(w2, period_mode="calendar")
    pay(w2, m2, 600_000, D(2026, 10, 6))
    assert row_of(w2, s2)["paid_through_forecast"] is None


def test_1_the_forecast_is_in_the_status_list_and_not_in_the_receipt(pg_session, clock):
    w = build_org(pg_session)
    _, s, m = package_world(w, clock)
    page = client(w, "owner").get(f"{API}/org/payments/status").json()
    item = next(i for i in page["items"] if i["membership_id"] == str(m.id))
    assert item["paid_through_forecast"] == "2026-10-30"


# ---- Part 2: a day off of the centre ----------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def five_groups(pg_session, clock, monkeypatch):
    """Five groups with a lesson on Wed 21.10; the exam-service refuses the lesson of the group 5 (an exam is running)."""
    w = build_org(pg_session)
    clock.set("2026-10-19 12:00")
    gs, ss, ms = centre(w, groups=5)
    blocked = str(gs[4].id)

    def check(self, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
        if phase == "cancellation-check" and group_id == blocked and lesson_date == "2026-10-21":
            raise ValueError("У урока идёт экзамен")

    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", check)
    return w, gs, ss, ms


DAY_OFF = {"dateFrom": "2026-10-21", "dateTo": "2026-10-21", "reason": "Хайит"}


def lessons_on(w, day="2026-10-21"):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(lesson_date=day, org_id=w.org.id).all()


def test_4_five_groups_one_with_a_running_exam(five_groups):
    w, gs, ss, ms = five_groups
    c = client(w, "owner")
    plan = c.post(f"{API}/org/days-off/preview", json=DAY_OFF)
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    assert len(plan["cancellable"]) == 4 and plan["groups"] == 4 and [b["groupName"] for b in plan["blocked"]] == ["Группа 5"]
    assert plan["blocked"][0]["reason"] == "У урока идёт экзамен"
    assert plan["text"].startswith("Будут отменены 4 урока в 4 группах: Группа 1")
    assert lessons_on(w) == [] and w.db.query(OrgDayOff).filter_by(org_id=w.org.id).count() == 0               # the dry run wrote nothing
    out = c.post(f"{API}/org/days-off", json=DAY_OFF)
    assert out.status_code == 201, out.text
    out = out.json()
    assert len(out["cancelled"]) == 4 and [b["groupName"] for b in out["blocked"]] == ["Группа 5"] and out["dayOff"]["reason"] == "Хайит"
    done = lessons_on(w)
    assert len(done) == 4 and {r.status for r in done} == {"cancelled"} and {r.cancellation_kind for r in done} == {"holiday"}
    assert {r.cancellation_reason for r in done} == {"Выходной (Хайит)"}              # what the students read
    assert {str(r.day_off_id) for r in done} == {out["dayOff"]["id"]}
    assert str(gs[4].id) not in {str(r.group_id) for r in done}
    # one road: the usual cancellation wrote its journal row too
    assert w.db.query(GroupAttendanceAudit).filter(GroupAttendanceAudit.org_id == w.org.id, GroupAttendanceAudit.action == "cancelled", GroupAttendanceAudit.cancellation_reason == "Выходной (Хайит)").count() == 4
    journal = c.get(f"{API}/org/days-off").json()
    assert [(j["id"], j["active"]) for j in journal] == [(out["dayOff"]["id"], True)]
    # the students' journal shows the reason
    att = client(w, "owner").get(f"{API}/org/groups/{gs[0].id}/attendance", params={"month": "2026-10"}).json()
    assert att["cancellationReasons"]["2026-10-21_lesson-wed"] == "Выходной (Хайит)"
    # the student's card: «Урок отменён» with the reason of the day off (the reason of any other cancellation is never sent); the group with the exam keeps its lesson
    card = client(w, "admin").get(f"{API}/org/students/{ss[0].id}/attendance", params={"month": "2026-10"}).json()
    assert [(n["date"], n["kind"], n["holiday"]) for n in card["notices"]] == [("2026-10-21", "cancelled", "Хайит")]
    assert client(w, "admin").get(f"{API}/org/students/{ss[4].id}/attendance", params={"month": "2026-10"}).json()["notices"] == []


def test_4_rights_and_input(five_groups):
    w, gs, ss, ms = five_groups
    assert client(w, "A").post(f"{API}/org/days-off/preview", json=DAY_OFF).status_code == 403          # a teacher: no
    assert client(w, "A").post(f"{API}/org/days-off", json=DAY_OFF).status_code == 403
    assert client(w, "admin").post(f"{API}/org/days-off/preview", json=DAY_OFF).status_code == 200      # an admin: yes
    c = client(w, "owner")
    assert c.post(f"{API}/org/days-off", json={**DAY_OFF, "reason": "ab"}).status_code == 422             # >= 3 characters (the dry run asks no reason)
    assert c.post(f"{API}/org/days-off/preview", json={k: v for k, v in DAY_OFF.items() if k != "reason"}).status_code == 200
    assert c.post(f"{API}/org/days-off/preview", json={**DAY_OFF, "dateTo": "2026-11-04"}).status_code == 422       # 15 days
    assert c.post(f"{API}/org/days-off/preview", json={**DAY_OFF, "dateTo": "2026-10-20"}).status_code == 422
    assert c.post(f"{API}/org/days-off/preview", json={**DAY_OFF, "dateTo": "2026-11-03"}).status_code == 200       # 14 days


def test_4_a_range_cancels_every_lesson_of_it(five_groups):
    w, gs, ss, ms = five_groups
    out = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-19", "dateTo": "2026-10-23", "reason": "Навруз"}).json()
    # the clock is Monday 12:00: Monday's lessons (10:00) are past - a day off only lists them (W1 S10, Д23); Wed and Fri x 5 groups are cancelled, minus the one with the exam
    assert len(out["cancelled"]) == 2 * 5 - 1
    assert sorted(b["reason"] for b in out["blocked"]) == sorted(["У урока идёт экзамен"] + ["Урок уже начался — отмените вручную, если не состоялся"] * 5)


def test_5_voiding_the_day_off_gives_back_its_own_untouched_lessons(five_groups):
    w, gs, ss, ms = five_groups
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY_OFF).json()
    day_off = out["dayOff"]["id"]
    touched = next(r for r in lessons_on(w) if r.group_id == gs[1].id)
    touched.version = 2                                                         # somebody changed the lesson after the day off
    w.db.commit()
    dry = c.post(f"{API}/org/days-off/{day_off}/void", params={"dry_run": "true"}).json()
    assert dry["dryRun"] is True and len(dry["restored"]) == 3 and [k["groupName"] for k in dry["kept"]] == ["Группа 2"] and len(lessons_on(w)) == 4
    voided = c.post(f"{API}/org/days-off/{day_off}/void")
    assert voided.status_code == 200, voided.text
    voided = voided.json()
    assert len(voided["restored"]) == 3 and [k["groupName"] for k in voided["kept"]] == ["Группа 2"] and voided["dayOff"]["active"] is False
    assert voided["kept"][0]["reason"] == "Урок изменён после выходного"
    left = lessons_on(w)
    assert [r.group_id for r in left] == [gs[1].id] and left[0].status == "cancelled"        # the others are scheduled again
    assert c.post(f"{API}/org/days-off/{day_off}/void").status_code == 409                    # once
    assert c.get(f"{API}/org/days-off/for-date", params={"date": "2026-10-21"}).json() == {"dayOff": None}


def test_5_a_lesson_of_the_past_or_a_foreign_cancellation_is_not_given_back(five_groups, clock):
    w, gs, ss, ms = five_groups
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY_OFF).json()
    clock.set("2026-10-22 09:00")                                              # the day off is over
    voided = c.post(f"{API}/org/days-off/{out['dayOff']['id']}/void").json()
    assert voided["restored"] == [] and {k["reason"] for k in voided["kept"]} == {"Урок уже в прошлом"} and len(lessons_on(w)) == 4


def test_6_a_lesson_created_on_a_day_off_is_a_warning_not_a_block(five_groups):
    w, gs, ss, ms = five_groups
    c = client(w, "owner")
    out = c.post(f"{API}/org/days-off", json=DAY_OFF).json()
    hint = c.get(f"{API}/org/days-off/for-date", params={"date": "2026-10-21"}).json()["dayOff"]
    assert hint["reason"] == "Хайит" and hint["id"] == out["dayOff"]["id"]
    assert client(w, "A").get(f"{API}/org/days-off/for-date", params={"date": "2026-10-21"}).json()["dayOff"]["reason"] == "Хайит"   # a teacher sees the hint too
    assert c.get(f"{API}/org/days-off/for-date", params={"date": "2026-10-22"}).json() == {"dayOff": None}
    r = c.post(f"{API}/org/schedule/occurrences", json={"groupId": str(gs[0].id), "lessonDate": "2026-10-21", "startTime": "15:00", "endTime": "16:00"})
    assert r.status_code == 201, r.text                                           # created: the screen warns, the server does not refuse
