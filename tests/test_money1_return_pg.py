"""Money-1 A2 (+A3): return of a held lesson, return of «Урока не было» to «Требует разбора», the impact of a correction (PostgreSQL).

Numbers: ``area-attendance-devil.md`` Д2 / П1 (a lesson held by a slip, a lesson cancelled by a mistake: 10 students x 75 000 = 750 000 deduction, 200 000 pay of the teacher)."""
from datetime import date

import pytest

from src.models.auth import GroupAttendance, GroupAttendanceAudit

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent, reopen_now  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import ensure_billing, make_client, needs_pg, pg_session, save, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import set_settings

pytestmark = needs_pg
API = "/api/v1/auth/org/groups"
OCT = date(2026, 10, 1)


def row_of(w, day, lid):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=w.group.id, lesson_date=day.isoformat(), lesson_id=lid).first()


def post_return(w, who, day, lid, **extra):
    """The screen sends the version it saw (R3 #11): by default the current one."""
    if "expectedVersion" not in extra and not extra.get("dryRun"):
        row = row_of(w, day, lid)
        extra["expectedVersion"] = int(row.version) if row is not None else 1
    return make_client(w, user=who).post(f"{API}/{w.group.id}/attendance/return", json={"lessonDate": day.isoformat(), "lessonId": lid, "reason": "Завершили по ошибке", **extra})


def package_centre(w):
    """The students pay by LESSONS (a package of 12): the only mode in which a held lesson spends a pack (area-attendance.md 4.4)."""
    set_settings(w, period_mode="lessons", lessons_per_payment=12)
    ensure_billing(w)


def test_the_owner_returns_a_held_lesson_the_pack_and_the_pay_are_given_back_and_the_audit_says_why(world, now_4_nov):
    package_centre(world)
    day, lid = hold_lesson(world, date(2026, 10, 28))
    sheet(world, world.teacher, OCT, tashkent(2026, 10, 30))                      # the sheet is saved mid-month: the month is OPEN
    r = post_return(world, world.owner, day, lid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["status"] == "in_progress" and body["version"] == 3
    effects = body["moneyEffects"]
    assert sorted(s["delta"] for s in effects["students"]) == [1, 1, 1]           # three students get the lesson back into the pack
    assert effects["payroll"] == []                                                # the sheet of this test is hand-made (no rates): nothing to measure; the chain of ``test_money1_holes_pg`` has the real numbers
    assert "в пакеты учеников вернётся занятие: 3" not in effects["text"] and "возвращено занятие: 3" in effects["text"]
    row = row_of(world, day, lid)
    assert row.status == "in_progress" and row.completed_at is None and {k: v for k, v in row.records.items()} != {}
    audit = world.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id, action="completed_reverted").one()
    assert audit.correction_reason == "Завершили по ошибке" and audit.actor_id == world.owner.id and audit.before_status == "completed"


def test_the_return_is_idempotent_and_has_a_dry_run(world, now_4_nov):
    package_centre(world)
    day, lid = hold_lesson(world, date(2026, 10, 28))
    dry = post_return(world, world.owner, day, lid, dryRun=True).json()
    assert dry["dryRun"] is True and dry["status"] == "completed" and row_of(world, day, lid).status == "completed"       # nothing written
    assert dry["moneyEffects"]["dryRun"] is True and "вернётся занятие: 3" in dry["moneyEffects"]["text"]
    assert post_return(world, world.owner, day, lid).json()["changed"] is True
    again = post_return(world, world.owner, day, lid).json()
    assert again["changed"] is False and again["status"] == "in_progress"
    assert world.db.query(GroupAttendanceAudit).filter_by(attendance_id=row_of(world, day, lid).id, action="completed_reverted").count() == 1        # I-7: one record


def test_a_stale_version_is_a_conflict(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    r = post_return(world, world.owner, day, lid, expectedVersion=1)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ATTENDANCE_CONFLICT"
    assert row_of(world, day, lid).status == "completed"


def test_only_the_money_right_returns_a_lesson_and_a_closed_month_needs_the_reopening_first(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    assert post_return(world, world.admin, day, lid).status_code == 403            # an administrator without the right
    assert post_return(world, world.teacher, day, lid).status_code == 403
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    shut = post_return(world, world.owner, day, lid)
    assert shut.status_code == 409 and shut.json()["detail"]["code"] == "PERIOD_CLOSED" and row_of(world, day, lid).status == "completed"
    grant_money_right(world)
    from src.auth import closed_period

    reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Урок завершили по ошибке")
    assert post_return(world, world.admin, day, lid).status_code == 200            # the administrator WITH the right, the month reopened


def test_the_impact_of_a_correction_names_the_students_whose_pack_changes_without_writing(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    s1, s2, s3 = world.students
    r = make_client(world, user=world.owner).post(f"{API}/{world.group.id}/attendance/impact", json={
        "lessonDate": day.isoformat(), "lessonId": lid, "records": {str(s1.id): "excused", str(s2.id): "present", str(s3.id): "absent"}})
    assert r.status_code == 200, r.text
    # «Уважительная» without a decision still counts (rule 4.3): nothing changes for the packs; a pending excuse changes the pay only after the decision
    assert r.json()["moneyEffects"]["students"] == []
    assert make_client(world, user=world.admin).post(f"{API}/{world.group.id}/attendance/impact", json={"lessonDate": day.isoformat(), "lessonId": lid, "records": {}}).status_code == 403
    assert row_of(world, day, lid).records[str(s1.id)] == "present"


def test_a_mistaken_lesson_not_held_goes_back_to_review_for_the_money_right_only(world, now_4_nov):
    from src.auth.schemas import ReconcileMissedLessonRequest

    day, lid = lesson(date(2026, 10, 28))
    world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Нажали по ошибке"),
                                             cancelled_by=str(world.teacher.id), is_org_admin=False, is_org_manager=False)
    assert row_of(world, day, lid).status == "cancelled"
    url = f"{API}/{world.group.id}/lessons/restore"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "reason": "Урок был, нажали по ошибке"}
    denied = make_client(world, user=world.admin).post(url, json=body)             # an administrator without the right: the old refusal, with the text that tells who can
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "RESTORE_PAST" and "с доступом к деньгам" in denied.json()["detail"]["message"]
    ok = make_client(world, user=world.owner).post(url, json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["returnedToReview"] is True and "moneyEffects" in ok.json()
    assert row_of(world, day, lid) is None                                          # the lesson is «Требует разбора» again: the teacher can reconcile it
    assert world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), __import__("src.auth.schemas", fromlist=["x"]).StartAttendanceRequest(lessonDate=day, lessonId=lid),
                                                       reconciled_by=str(world.teacher.id), is_org_admin=False) == 1


def test_a_centre_that_pays_by_the_calendar_has_no_pack_to_give_back(world, now_4_nov):
    """area-attendance.md 4.4: with a calendar payment a mark changes nothing for the student; only the sheet of the teacher is named."""
    day, lid = hold_lesson(world, date(2026, 10, 28))
    r = post_return(world, world.owner, day, lid, dryRun=True).json()
    assert r["moneyEffects"]["students"] == [] and "пакет" not in r["moneyEffects"]["text"]


def test_r3_11_the_return_needs_the_version_and_a_late_duplicate_cannot_return_the_lesson_the_teacher_finished_again(world, now_4_nov):
    """No version -> 422 (a client that does not send it cannot undo a re-finished lesson). A duplicate of the first request (version v), arriving after the
    owner returned the lesson and the teacher finished it AGAIN (version v+2), is a conflict - not a second return."""
    package_centre(world)
    day, lid = hold_lesson(world, date(2026, 10, 28))
    url = f"{API}/{world.group.id}/attendance/return"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "reason": "Завершили по ошибке"}
    missing = make_client(world, user=world.owner).post(url, json=body)
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "VERSION_REQUIRED"
    assert row_of(world, day, lid).status == "completed"
    seen = int(row_of(world, day, lid).version)
    assert post_return(world, world.owner, day, lid, expectedVersion=seen).json()["changed"] is True
    row = row_of(world, day, lid)
    row.status, row.version = "completed", int(row.version) + 1                            # the teacher finished it again
    world.db.commit()
    late = post_return(world, world.owner, day, lid, expectedVersion=seen)
    assert late.status_code == 409 and late.json()["detail"]["code"] == "ATTENDANCE_CONFLICT" and late.json()["detail"]["currentVersion"] == seen + 2
    assert row_of(world, day, lid).status == "completed"                                    # untouched
