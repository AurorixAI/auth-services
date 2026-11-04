"""Money-1 A1: the closed period (variant C), the reopening, and every place that used to have its own rule (PostgreSQL).

Numbers of the analysis: ``area-payroll-devil.md`` ЗПД-2 (the table of days), ``area-attendance-devil.md`` ОШ-3 (three rules for one sum)."""
from datetime import date

import pytest

from src.auth import closed_period
from src.auth.ops_rules import OpsError
from src.auth.schemas import ReconcileMissedLessonRequest, StartAttendanceRequest
from src.models.auth import PayrollPeriodReopening

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent, reopen_now  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, save, world  # noqa: F401  (fixtures)

pytestmark = needs_pg

OCT = date(2026, 10, 1)
MARKS_ABSENT_FIRST = lambda w: [(w.students[0], "absent"), (w.students[1], "present"), (w.students[2], "present")]


def reconcile(w, day, lid):
    return w.svc.reconcile_missed_group_attendance(str(w.org.id), str(w.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid),
                                                   reconciled_by=str(w.admin.id), is_org_admin=True)


def code(exc_info):
    return exc_info.value.detail["code"]


# ---------------------------------------------------------------------------------------------------- the rule (the table of ЗПД-2)
@pytest.mark.parametrize("saved, closed", [
    (tashkent(2026, 10, 15), False),          # an advance on the 15th: the month stays open
    (tashkent(2026, 10, 31, 18), False),      # the payday itself, before midnight
    (tashkent(2026, 11, 1, 1), True),         # 01:00 on the 1st by Tashkent = 20:00 UTC of the 31st: the UTC trap, the business time says «after the month»
    (tashkent(2026, 11, 2), True),
])
def test_a_month_is_closed_when_its_last_sheet_was_saved_after_the_month_ended(world, now_4_nov, saved, closed):
    sheet(world, world.teacher, OCT, saved)
    state = closed_period.period_state(world.db, world.org.id, world.teacher.id, OCT)
    assert (state["state"] == "closed") is closed


def test_the_old_window_is_gone_a_lesson_of_an_unsaved_month_can_be_reconciled_at_any_age(world, now_4_nov):
    """Before: LESSON_OUTSIDE_WINDOW for a lesson older than the 1st of the previous month (04.11 -> before 01.10).  Now nobody's sheet protects it."""
    day, lid = lesson(date(2026, 8, 12))
    assert reconcile(world, day, lid) == 1


def test_a_closed_month_refuses_the_reconcile_of_its_lesson_with_the_reason_and_the_date(world, now_4_nov):
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    day, lid = lesson(date(2026, 10, 28))
    with pytest.raises(OpsError) as e:
        reconcile(world, day, lid)
    assert e.value.status_code == 409 and code(e) == "PERIOD_CLOSED"
    assert e.value.detail["month"] == "2026-10" and e.value.detail["closedOn"] == "2026-11-02"
    assert "переоткрывает месяц" in e.value.detail["message"] and "октябрь 2026" in e.value.detail["message"]


def test_the_sheet_of_another_teacher_does_not_close_this_lesson(world, now_4_nov):
    sheet(world, world.teacher2, OCT, tashkent(2026, 11, 2))
    day, lid = lesson(date(2026, 10, 28))
    assert reconcile(world, day, lid) == 1


def test_a_mid_month_sheet_does_not_close_the_month_for_the_teacher(world, now_4_nov):
    sheet(world, world.teacher, OCT, tashkent(2026, 10, 15))
    day, lid = lesson(date(2026, 10, 28))
    assert reconcile(world, day, lid) == 1


def test_cancel_missed_is_refused_in_a_closed_month(world, now_4_nov):
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    day, lid = lesson(date(2026, 10, 28))
    with pytest.raises(OpsError) as e:
        world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id),
                                                 ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Не было"),
                                                 cancelled_by=str(world.admin.id), is_org_admin=True, is_org_manager=True)
    assert code(e) == "PERIOD_CLOSED"


def test_a_held_lesson_cannot_be_corrected_in_a_closed_month_and_there_was_no_age_limit_before(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    with pytest.raises(OpsError) as e:
        save(world, day, lid, MARKS_ABSENT_FIRST(world), status="completed", correctionReason="ошибся")
    assert code(e) == "PERIOD_CLOSED"


def test_a_decision_on_an_excuse_is_refused_in_a_closed_month(world, now_4_nov):
    from src.auth.attendance_excuses import AttendanceExcuseService
    from src.models.auth import AttendanceExcuse
    from tests.money1_helpers import lesson as _l
    from src.auth.schemas import StartAttendanceRequest as S

    day, lid = _l(date(2026, 10, 28))
    world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), S(lessonDate=day, lessonId=lid), reconciled_by=str(world.admin.id), is_org_admin=True)
    s1, s2, s3 = world.students
    save(world, day, lid, [(s1, "excused"), (s2, "present"), (s3, "present")], status="completed", excuseReasons={s1.id: "Болел, написал в Telegram"})
    excuse = world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id).one()
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    with pytest.raises(OpsError) as e:
        AttendanceExcuseService(world.db).review(str(world.org.id), excuse.id, str(world.admin.id), "confirmed", None)
    assert code(e) == "PERIOD_CLOSED"
    reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Справка пришла позже")
    assert AttendanceExcuseService(world.db).review(str(world.org.id), excuse.id, str(world.admin.id), "confirmed", None).status == "confirmed"


# ---------------------------------------------------------------------------------------------------- the reopening
def test_reopen_opens_the_month_writes_the_journal_is_idempotent_and_the_next_save_closes_it_again(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    row = sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    first = reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Ошибка в отметке")
    assert first["changed"] is True and first["state"] == "reopened" and first["reopened"]["reason"] == "Ошибка в отметке"
    again = reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Ошибка в отметке")
    assert again["changed"] is False
    assert world.db.query(PayrollPeriodReopening).filter_by(org_id=world.org.id).count() == 1                      # I-7: a repeat writes nothing
    save(world, day, lid, MARKS_ABSENT_FIRST(world), status="completed", correctionReason="ошибся")      # allowed while reopened
    row.updated_at = tashkent(2026, 11, 4, 12, 5)                                   # the owner saves the sheet again
    world.db.commit()
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, OCT)["state"] == "closed"


def test_reopen_needs_a_reason_and_a_sheet(world, now_4_nov):
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    with pytest.raises(OpsError) as e:
        reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "  ")
    assert code(e) == "REASON_REQUIRED"
    with pytest.raises(OpsError) as e:
        reopen_now(world.db, world.org.id, world.teacher2.id, "2026-10", world.owner.id, "Причина есть")
    assert code(e) == "SHEET_NOT_FOUND"


def test_the_reopen_route_is_for_the_money_right_only(world, now_4_nov):
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    body = {"teacher_id": str(world.teacher.id), "reason": "Ошибка в отметке", "sheet_revision": 1}
    url = "/api/v1/auth/org/payroll-periods/2026-10/reopen"
    assert make_client(world, user=world.admin).post(url, json=body).status_code == 403          # an administrator without the right
    assert make_client(world, user=world.teacher).post(url, json=body).status_code == 403
    grant_money_right(world)
    ok = make_client(world, user=world.admin).post(url, json=body)
    assert ok.status_code == 200 and ok.json()["state"] == "reopened"
    listing = make_client(world, user=world.admin).get("/api/v1/auth/org/payroll-periods?month=2026-10").json()
    assert listing["items"][0]["state"] == "reopened" and listing["items"][0]["reopened"]["reason"] == "Ошибка в отметке"


# ---------------------------------------------------------------------------------------------------- the other places
def test_change_of_the_lesson_teacher_asks_the_new_teacher_too(world, now_4_nov):
    from src.auth.lesson_teachers import LessonTeachers
    from src.auth.ops_schemas import LessonTeacherPut

    day, lid = hold_lesson(world, date(2026, 10, 28))
    sheet(world, world.teacher2, OCT, tashkent(2026, 11, 2))         # only the NEW teacher's month is closed
    with pytest.raises(OpsError) as e:
        LessonTeachers(world.svc).put(world.org.id, world.group.id, LessonTeacherPut(lessonDate=day, lessonId=lid, teacherId=str(world.teacher2.id), reason="Заменил коллега"),
                                      actor_id=world.admin.id, actor_role="admin")
    assert code(e) == "PERIOD_CLOSED"


def test_cancellation_kind_of_a_closed_month_does_not_change(world, now_4_nov):
    from src.auth.cancellation_kind import ChangeCancellationKind, change_cancellation_kind

    day, lid = lesson(date(2026, 10, 27))
    world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Нет света"),
                                             cancelled_by=str(world.admin.id), is_org_admin=True, is_org_manager=True)
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    with pytest.raises(OpsError) as e:
        change_cancellation_kind(world.db, str(world.org.id), str(world.group.id),
                                 ChangeCancellationKind(lessonDate=day, lessonId=lid, cancellationKind="holiday", reason="Праздник"), actor_id=str(world.admin.id))
    assert code(e) == "PERIOD_CLOSED"


def test_a_pause_over_a_closed_month_is_refused_and_one_older_than_30_days_in_an_open_month_is_allowed(world, now_4_nov):
    """Before: PAUSE_RANGE «не раньше чем 30 дней назад» for both."""
    from src.auth.logica_service import PauseLocked, PauseService
    from src.auth.payments_schemas import PauseCreate
    from src.models.auth import StudentGroup

    membership = world.db.query(StudentGroup).filter_by(student_id=world.students[0].id).one()
    service = PauseService(world.db)
    sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 10, 3))            # September is CLOSED
    with pytest.raises(PauseLocked) as e:
        service.create(str(world.org.id), membership.id, str(world.admin.id), PauseCreate(first_day=date(2026, 9, 24), last_day=date(2026, 9, 30), reason="Болезнь"))
    assert e.value.extra["month"] == "2026-09"
    ok = service.create(str(world.org.id), membership.id, str(world.admin.id), PauseCreate(first_day=date(2026, 10, 1), last_day=date(2026, 10, 3), reason="Болезнь"))
    assert ok.pause.first_day == date(2026, 10, 1)                                  # 34 days back, October is open: allowed


def test_r3_11_a_late_duplicate_of_the_reopening_is_refused_by_the_revision_and_the_revision_is_required(world, now_4_nov):
    """The owner reopened (rev 1), fixed, saved the sheet again (rev 2) - the month is closed again; a late duplicate of the first request (rev 1) must NOT reopen it a second time."""
    row = sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    first = closed_period.reopen_month(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Исправляем ошибку", 1)
    assert first["changed"] is True and first["sheetRevision"] == 1
    row.revision, row.updated_at = 2, tashkent(2026, 11, 4, 12, 5)                  # the owner saved the sheet again
    world.db.commit()
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, OCT)["state"] == "closed"
    with pytest.raises(OpsError) as late:
        closed_period.reopen_month(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Исправляем ошибку", 1)
    assert late.value.status_code == 409 and code(late) == "SHEET_CHANGED" and late.value.detail["currentRevision"] == 2
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, OCT)["state"] == "closed"        # still closed
    with pytest.raises(OpsError) as missing:
        closed_period.reopen_month(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Исправляем ошибку", None)
    assert missing.value.status_code == 422 and code(missing) == "REVISION_REQUIRED"


def test_r3_m1_no_reason_is_our_sentence_not_the_english_422_of_the_schema(world, now_4_nov):
    """«нет причины» - an empty string AND spaces, over HTTP (R3 m1 / #13)."""
    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    url = "/api/v1/auth/org/payroll-periods/2026-10/reopen"
    client = make_client(world, user=world.owner)
    for reason in ("", "   ", "ab"):
        r = client.post(url, json={"teacher_id": str(world.teacher.id), "reason": reason, "sheet_revision": 1})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED" and "Причина" in r.json()["detail"]["message"], (reason, r.text)
    bad_month = client.post("/api/v1/auth/org/payroll-periods/2026-1/reopen", json={"teacher_id": str(world.teacher.id), "reason": "Исправляем ошибку", "sheet_revision": 1})
    assert bad_month.status_code == 422 and bad_month.json()["detail"]["code"] == "MONTH_INVALID" and "ГГГГ-ММ" in bad_month.json()["detail"]["message"]


def test_r3_n1_every_code_of_the_closed_period_is_in_the_catalogue_with_the_status_the_code_gives():
    """The catalogue is what the contract exports: a code the screens meet (reopening, return of a lesson, restore) and the status the route answers with must be in it."""
    from src.auth.ops_rules import ERROR_CATALOGUE

    for code, status in (("SHEET_NOT_FOUND", 404), ("REASON_REQUIRED", 422), ("LESSON_IN_PROGRESS", 409), ("MONTH_INVALID", 422), ("REVISION_REQUIRED", 422), ("SHEET_CHANGED", 409),
                         ("VERSION_REQUIRED", 422), ("RESTORE_BEFORE_START", 409), ("PERIOD_CLOSED", 409), ("MONEY_RIGHT_REQUIRED", 403)):
        assert ERROR_CATALOGUE[code][0] == status and ERROR_CATALOGUE[code][1], code


def test_r3_m9_a_correction_that_passed_the_check_holds_the_sheet_of_the_month_until_it_ends(world, now_4_nov):
    """The check «the month is open» takes the saved sheets FOR SHARE: the save of the sheet (FOR UPDATE) cannot slip in between the check and the correction."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    row = sheet(world, world.teacher, OCT, tashkent(2026, 10, 15))                                    # a sheet saved mid-month: the month is open
    closed_period.ClosedPeriods.of(world.db, world.org.id).assert_open([world.teacher.id], date(2026, 10, 28))        # the check of a correction ... the transaction goes on
    engine = world.db.get_bind()
    engine = getattr(engine, "engine", engine)
    with Session(engine) as other:
        other.execute(text("SET lock_timeout = '300ms'"))
        with pytest.raises(OperationalError):                                                         # the save of the sheet has to wait
            other.execute(text("SELECT id FROM payroll_sheets WHERE id = :i FOR UPDATE"), {"i": row.id})
        other.rollback()
    world.db.rollback()                                                                               # the correction is done: the hold is let go
    with Session(engine) as other:
        other.execute(text("SET lock_timeout = '300ms'"))
        assert other.execute(text("SELECT id FROM payroll_sheets WHERE id = :i FOR UPDATE"), {"i": row.id}).first() is not None
        other.rollback()


def test_r3_n3_the_list_of_periods_costs_the_same_number_of_queries_for_one_teacher_or_many(world, now_4_nov):
    from tests.pg_world import capture_queries, make_member, make_user

    sheet(world, world.teacher, OCT, tashkent(2026, 11, 2))
    few, listed = capture_queries(world.db, lambda: closed_period.list_periods(world.db, world.org.id, "2026-10"))
    assert len(listed) == 1 and listed[0]["state"] == "closed"
    for n in range(6):                                                                               # six more teachers with a sheet each (some reopened)
        teacher = make_user(world.db, "teacher", f"Учитель{n}")
        make_member(world.db, world.org, teacher, "teacher")
        sheet(world, teacher, OCT, tashkent(2026, 11, 2))
        if n % 2:
            reopen_now(world.db, world.org.id, teacher.id, "2026-10", world.owner.id, "Правим ведомость")
    many, listed = capture_queries(world.db, lambda: closed_period.list_periods(world.db, world.org.id, "2026-10"))
    assert len(listed) == 7 and {x["state"] for x in listed} == {"closed", "reopened"}
    assert len(many) == len(few) <= 4, (len(few), len(many))             # the same few queries for 1 or 7 teachers
