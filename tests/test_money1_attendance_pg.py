"""Money-1 A3 / A4 / A5 / A6 on the lesson journal: who may correct a held lesson, what the answer says about money, the teacher's own correction, the nobody-came gate, «кто провёл» (PostgreSQL).

Numbers: ``area-attendance-devil.md`` Д1 / T27 (a paid month, a correction -> 'Переплата'), ЗПД-6 (+25 000 x 6 pupils for a teacher who did not teach)."""
from datetime import date

import pytest

from src.auth.ops_rules import OpsError
from src.auth.schemas import StartAttendanceRequest
from src.models.auth import GroupAttendance, GroupAttendanceAudit, PayrollPayout

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, save, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth/org/groups"
OCT = date(2026, 10, 1)


def marks(w, *values):
    return list(zip(w.students, values))


def post_save(w, who, day, lid, values, **extra):
    return make_client(w, user=who).post(f"{API}/{w.group.id}/attendance", json={
        "lessonDate": day.isoformat(), "lessonId": lid, "status": "completed", "records": {str(s.id): v for s, v in zip(w.students, values)}, **extra})


def test_an_administrator_without_the_money_right_cannot_correct_a_held_lesson_the_one_with_it_can_and_hears_the_consequences(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1, s2, s3 = world.students
    sheet(world, world.teacher, OCT, tashkent(2026, 10, 30), total=1_000_000)       # a saved sheet of October (the month is open)
    refused = post_save(world, world.admin, day, lid, ["excused", "present", "present"], correctionReason="Болел", excuseReasons={str(s1.id): "Болел, написал"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    grant_money_right(world)
    done = post_save(world, world.admin, day, lid, ["absent", "present", "present"], correctionReason="Опечатка")
    assert done.status_code == 200, done.text
    effects = done.json()["moneyEffects"]
    assert effects["payroll"] == [{"month": "2026-10", "state": "open"}]            # an administrator: the month only, no sums of the teacher's pay (LESSONS 45)
    assert "Ведомость" in effects["text"] and "изменилась" in effects["text"]
    # the owner hears the sums: the saved total, and what the data says now
    again = post_save(world, world.owner, day, lid, ["present", "present", "present"], correctionReason="Вернули")
    mine = again.json()["moneyEffects"]["payroll"][0]
    assert mine["savedTotal"] == 1_000_000 and mine["state"] == "open" and "freshTotal" in mine and "changedBy" in mine and mine["teacherName"]


def test_a_correction_that_changes_what_a_pack_spends_names_the_students(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1 = world.students[0]
    grant_money_right(world)
    # a CONFIRMED excuse gives the lesson back to the pack of the student: here a new «excused» is pending (still spends), so a change to «absent -> excused» alone changes nothing
    r = post_save(world, world.owner, day, lid, ["excused", "present", "present"], correctionReason="Справка", excuseReasons={str(s1.id): "Болел, написал"})
    assert r.status_code == 200 and r.json()["moneyEffects"]["students"] == []


def test_the_teacher_corrects_his_own_held_lesson_within_seven_days_in_an_open_month_with_a_reason(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    teacher = make_client(world, user=world.teacher)
    url = f"{API}/{world.group.id}/attendance"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "status": "completed", "records": {str(s.id): v for s, v in zip(world.students, ["absent", "present", "present"])}}
    no_reason = teacher.post(url, json=body)
    assert no_reason.status_code == 400 and "причину" in no_reason.json()["detail"]
    ok = teacher.post(url, json={**body, "correctionReason": "Ошибся в отметке"})
    assert ok.status_code == 200, ok.text
    assert "payroll" in ok.json()["moneyEffects"] and "teacherName" not in str(ok.json()["moneyEffects"])      # the same warning, without the money of the teachers
    world.db.expire_all()
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one()
    assert list(row.records.values()).count("absent") == 1
    audit = world.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id, action="corrected").one()
    assert audit.actor_id == world.teacher.id and audit.correction_reason == "Ошибся в отметке"


def test_the_teachers_own_correction_has_its_limits_age_month_and_whose_lesson(world, now_4_nov):
    old_day, old_lid = hold_lesson(world, date(2026, 10, 27))                       # 8 days ago: too old for a teacher
    url = f"{API}/{world.group.id}/attendance"
    teacher = make_client(world, user=world.teacher)
    mk = lambda d, l: {"lessonDate": d.isoformat(), "lessonId": l, "status": "completed", "correctionReason": "Ошибся", "records": {str(s.id): v for s, v in zip(world.students, ["absent", "present", "present"])}}
    too_old = teacher.post(url, json=mk(old_day, old_lid))
    assert too_old.status_code == 403 and too_old.json()["detail"]["code"] == "LESSON_TOO_OLD"
    day, lid = hold_lesson(world, date(2026, 10, 30))
    other = make_client(world, user=world.teacher2).post(url, json=mk(day, lid))
    assert other.status_code == 403                                                  # a teacher of another group: no access to the lesson at all
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))                          # the owner saved October after it ended: closed
    shut = teacher.post(url, json=mk(day, lid))
    assert shut.status_code == 409 and shut.json()["detail"]["code"] == "PERIOD_CLOSED"


def test_completing_a_lesson_where_nobody_came_asks_for_a_yes(world, now_4_nov):
    day, lid = lesson(date(2026, 10, 30))
    world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(world.admin.id), is_org_admin=True)
    owner = make_client(world, user=world.owner)
    url = f"{API}/{world.group.id}/attendance"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "status": "completed", "records": {str(s.id): "absent" for s in world.students}}
    asked = owner.post(url, json=body)
    assert asked.status_code == 409 and asked.json()["detail"]["code"] == "NOBODY_CAME" and "оплачен преподавателю" in asked.json()["detail"]["message"]
    assert owner.post(url, json={**body, "confirmNobodyCame": True}).status_code == 200


def test_who_taught_a_lesson_formalised_backwards_is_chosen_by_the_money_right_and_written_to_the_journal(world, now_4_nov):
    """ЗПД-6: 21.10 A was ill, B taught, the administrator formalised the lesson afterwards: before, the planned teacher A got 6 x 25 000."""
    day, lid = lesson(date(2026, 10, 30))
    url = f"{API}/{world.group.id}/attendance/reconcile"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "taughtBy": str(world.teacher2.id)}
    denied = make_client(world, user=world.admin).post(url, json=body)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert make_client(world, user=world.admin).post(url, json={"lessonDate": day.isoformat(), "lessonId": lid}).status_code == 403             # not even the planned teacher's lesson, by somebody else
    ok = make_client(world, user=world.owner).post(url, json=body)
    assert ok.status_code == 200, ok.text
    world.db.expire_all()
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one()
    assert row.teacher_id == world.teacher2.id                                      # the pay goes to the teacher who taught, not to the planned one
    audit = world.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id, action="reconciliation_started").one()
    assert audit.details == {"backdated": True, "planned_teacher": str(world.teacher.id), "taught_by": str(world.teacher2.id)}


def test_a_stranger_cannot_be_named_as_the_teacher_who_taught(world, now_4_nov):
    day, lid = lesson(date(2026, 10, 30))
    r = make_client(world, user=world.owner).post(f"{API}/{world.group.id}/attendance/reconcile", json={"lessonDate": day.isoformat(), "lessonId": lid, "taughtBy": str(world.foreign_admin.id)})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TARGET_NOT_TEACHER"
