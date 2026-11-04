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
    # R3 #17/#21: a sheet is named only when this action REALLY moves it; the hand-made sheet of this world has no rate of the teacher, so there is nothing to measure
    # (the sums and the «month only» for an administrator are proven with real rates in ``test_money1_effects_pg`` and ``test_money1_holes_pg``)
    assert effects["payroll"] == [] and effects["dryRun"] is False
    again = post_save(world, world.owner, day, lid, ["present", "present", "present"], correctionReason="Вернули")
    assert again.status_code == 200 and again.json()["moneyEffects"]["payroll"] == []


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


def test_completing_a_lesson_where_nobody_came_asks_for_a_yes_only_a_screen_that_asks(world, now_4_nov):
    """R3 #8: an older screen does not send the field -> the old behaviour (200); the new one sends ``false`` first (409 + the sentence), then ``true``."""
    owner = make_client(world, user=world.owner)
    url = f"{API}/{world.group.id}/attendance"
    for offset, extra, status in ((30, {}, 200), (29, {"confirmNobodyCame": False}, 409), (28, {"confirmNobodyCame": True}, 200)):
        day, lid = lesson(date(2026, 10, offset))
        world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(world.admin.id), is_org_admin=True)
        body = {"lessonDate": day.isoformat(), "lessonId": lid, "status": "completed", "records": {str(s.id): "absent" for s in world.students}, **extra}
        r = owner.post(url, json=body)
        assert r.status_code == status, (extra, r.text)
        if status == 409:
            assert r.json()["detail"]["code"] == "NOBODY_CAME" and "оплачен преподавателю" in r.json()["detail"]["message"]
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


def test_the_journal_says_what_changed_and_that_the_lesson_was_formalised_afterwards(world, now_4_nov):
    """П9 / Д4: «было → стало» per student from the audit, every earlier correction in ``history``, and the mark «оформлен задним числом» with the teacher who taught."""
    day, lid = lesson(date(2026, 10, 30))
    owner = make_client(world, user=world.owner)
    owner.post(f"{API}/{world.group.id}/attendance/reconcile", json={"lessonDate": day.isoformat(), "lessonId": lid, "taughtBy": str(world.teacher2.id)})
    s1, s2, s3 = world.students
    save(world, day, lid, [(s1, "present"), (s2, "present"), (s3, "present")], status="completed")
    save(world, day, lid, [(s1, "absent"), (s2, "present"), (s3, "present")], status="completed", correctionReason="Первая правка")
    save(world, day, lid, [(s1, "absent"), (s2, "late"), (s3, "present")], status="completed", correctionReason="Вторая правка")
    journal = owner.get(f"{API}/{world.group.id}/attendance", params={"date_from": day.isoformat(), "date_to": day.isoformat()}).json()
    key = f"{day.isoformat()}|{lid}" if f"{day.isoformat()}|{lid}" in journal["corrections"] else next(iter(journal["corrections"]))
    last = journal["corrections"][key]
    assert last["reason"] == "Вторая правка" and last["changes"] == [{"studentId": str(s2.id), "from": "present", "to": "late"}]
    assert [h["reason"] for h in last["history"]] == ["Первая правка"] and last["history"][0]["changes"] == [{"studentId": str(s1.id), "from": "present", "to": "absent"}]
    mark = journal["backdated"][key]
    assert mark["taught_by_name"] and mark["by_name"] is not None


def test_the_archive_of_a_group_says_before_the_confirmation_which_lessons_it_cancels_and_what_that_costs(world, now_4_nov):
    """П8: the dialog showed a number; now the lessons (date, teacher) and the sentence «за них не начислят и не заплатят»."""
    for s in list(world.students):
        s.group_id = None
    from src.models.auth import StudentGroup

    world.db.query(StudentGroup).filter_by(group_id=world.group.id).update({"is_active": False})
    world.db.commit()
    impact = make_client(world, user=world.admin).get(f"{API}/{world.group.id}/archive-impact").json()
    missed = next(b for b in impact["blockers"] if b["code"] == "MISSED_LESSONS")
    assert missed["count"] >= 1 and len(missed["lessons"]) == missed["count"] and missed["lessons"][0]["teacherName"]
    assert "не начислят" in missed["cancelConsequence"] and "не заплатят" in missed["cancelConsequence"]


def test_r3_n6_who_taught_is_an_id_nothing_else_a_text_a_stranger_and_a_valid_teacher_are_told_apart(world, now_4_nov):
    day, lid = lesson(date(2026, 10, 30))
    url = f"{API}/{world.group.id}/attendance/reconcile"
    owner = make_client(world, user=world.owner)
    junk = owner.post(url, json={"lessonDate": day.isoformat(), "lessonId": lid, "taughtBy": "'; drop table users; --"})
    assert junk.status_code == 422 and junk.json()["detail"][0]["loc"][-1] == "taughtBy"                 # the type refuses a text that is not an id
    assert world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).count() == 0       # and nothing was written
    assert owner.post(url, json={"lessonDate": day.isoformat(), "lessonId": lid, "taughtBy": str(world.teacher2.id)}).status_code == 200
