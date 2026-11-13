"""Money-1 R3 #22-#28: what the screens of package A asked of the server - the sum BEFORE the confirmation (``freshTotal``), the dry run of «Вернуть в разбор», the impact for the teacher of his own
lesson, the mark of a returned lesson in the journal, the name of the group in the open lessons, the list of the decided additional lessons (PostgreSQL)."""
from datetime import date

import pytest

from src.auth import money_effects as me
from src.models.auth import GroupAttendance

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import ensure_billing, make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)

pytestmark = needs_pg
API = "/api/v1/auth/org/groups"


def cancel_by_centre(w, day):
    from src.auth.schemas import ReconcileMissedLessonRequest

    day, lid = lesson(day)
    w.svc.cancel_missed_group_attendance(str(w.org.id), str(w.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Нет света", cancellationKind="centre"),
                                         cancelled_by=str(w.admin.id), is_org_admin=True, is_org_manager=True, money_right=True)
    return day, lid


def test_r3_23_the_return_of_a_cancelled_lesson_has_a_dry_run_that_names_the_deduction_that_disappears(world, now_4_nov):
    ensure_billing(world)
    payments_block(world.db, world.org, recalc_history=[{"from": "min", "on": True}])            # the centre deducts the lessons it cancelled
    day, lid = cancel_by_centre(world, date(2026, 10, 28))
    before = me.centre_deductions(world.db, world.org.id, world.group.id)
    assert len(before) == 3 and all(v > 0 for v in before.values())                               # three students, each is deducted for the cancelled lesson
    owner = make_client(world, user=world.owner)
    url = f"{API}/{world.group.id}/lessons/restore"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "reason": "Свет дали, урок был"}
    dry = owner.post(url, json={**body, "dryRun": True})
    assert dry.status_code == 200, dry.text
    out = dry.json()
    assert out["dryRun"] is True and out["returnedToReview"] is True and out["moneyEffects"]["dryRun"] is True
    assert sorted(d["amount"] for d in out["deductions"]) == sorted(before.values()) and all(d["name"] for d in out["deductions"])      # the whole deduction for this lesson goes away
    total = sum(before.values())
    assert f"{total:,}".replace(",", "\u00a0") in out["moneyEffects"]["text"] and "снимем перерасчёт" in out["moneyEffects"]["text"]
    world.db.expire_all()
    assert world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one().status == "cancelled"        # nothing written
    assert me.centre_deductions(world.db, world.org.id, world.group.id) == before
    done = owner.post(url, json=body).json()
    assert done["dryRun"] is False and done["moneyEffects"]["dryRun"] is False and "снят перерасчёт" in done["moneyEffects"]["text"]
    assert done["deductions"] == out["deductions"]                                                  # what the confirmation said is what happened
    assert sum(me.centre_deductions(world.db, world.org.id, world.group.id).values()) == 0


def test_r3_24_the_teacher_of_a_held_lesson_asks_the_impact_of_his_own_correction_without_sums_nobody_else_without_the_right(world, fixed_today):
    """ЗП-05 world: 475 000 saved mid-month (open).  Taking the mark of S1 on the 13th away (he was not there at all) takes 25 000 off: the owner hears the sums, the teacher of the lesson only the month and the state."""
    from src.models.auth import PayrollSheet
    from tests.test_logicA_payroll_pg import g_pay_world, save_sheet

    m, s1, s2 = g_pay_world(world)
    saved = save_sheet(world, m)
    world.db.query(PayrollSheet).filter(PayrollSheet.id == saved["id"]).update({"updated_at": tashkent(m.first.year, m.first.month, 15)})        # saved mid-month: open
    world.db.commit()
    day = m.d(13)
    url = f"{API}/{world.group.id}/attendance/impact"
    body = {"lessonDate": day.isoformat(), "lessonId": f"s{day.isoweekday()}", "records": {str(s2.id): "present"}}
    mine = make_client(world, user=world.teacher).post(url, json=body)
    assert mine.status_code == 200, mine.text
    effects = mine.json()["moneyEffects"]
    assert effects["dryRun"] is True and effects["payroll"] == [{"month": m.label, "state": "open"}]          # the month and the state only: no sum of anybody's pay
    other = make_client(world, user=world.teacher2).post(url, json=body)                                       # another teacher, not the one of this lesson
    assert other.status_code == 403 and other.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert make_client(world, user=world.admin).post(url, json=body).status_code == 403                         # an administrator without the money right
    grant_money_right(world)
    assert make_client(world, user=world.admin).post(url, json=body).json()["moneyEffects"]["payroll"] == [{"month": m.label, "state": "open"}]
    (line,) = make_client(world, user=world.owner).post(url, json=body).json()["moneyEffects"]["payroll"]      # the owner: the sums
    assert (line["savedTotal"], line["freshBefore"], line["freshAfter"], line["changedBy"]) == (475_000, 475_000, 450_000, -25_000)


def test_r3_25_a_lesson_that_was_only_returned_is_marked_in_the_journal(world, now_4_nov):
    from src.auth.schemas import StartAttendanceRequest  # noqa: F401
    from tests.pg_world import save

    day, lid = hold_lesson(world, date(2026, 10, 28))
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one()
    owner = make_client(world, user=world.owner)
    r = owner.post(f"{API}/{world.group.id}/attendance/return", json={"lessonDate": day.isoformat(), "lessonId": lid, "reason": "Завершили по ошибке", "expectedVersion": int(row.version)})
    assert r.status_code == 200 and r.json()["changed"] is True
    save(world, day, lid, list(zip(world.students, ("present", "present", "present"))), status="completed")           # held AGAIN, no mark changed
    journal = owner.get(f"{API}/{world.group.id}/attendance", params={"month": "2026-10"}).json()
    entry = journal["corrections"][f"{day.isoformat()}_{lid}"]
    assert entry["kind"] == "returned" and entry["reason"] == "Завершили по ошибке" and entry["changes"] == [] and entry["by_name"]


def test_r3_15_a_transfer_has_a_dry_run_that_says_what_travels_and_writes_nothing(world, now_4_nov):
    from src.models.auth import StudentGroup
    from tests.pg_world import make_group
    from tests.test_payroll_pg import pay_calendar

    s0 = world.students[0]
    pay_calendar(world, s0, date(2026, 9, 1), date(2026, 11, 30))                                  # paid through the end of November
    other = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    grant_money_right(world)
    admin = make_client(world, user=world.admin)
    url = f"/api/v1/auth/org/groups/{other.id}/students/{s0.id}"
    dry = admin.post(url, params={"mode": "transfer", "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True and dry.json()["carried"], dry.json()                       # the sentence with the sum that would travel
    world.db.expire_all()
    mine = world.db.query(StudentGroup).filter_by(student_id=s0.id).all()
    assert [(m.group_id == world.group.id, m.is_active) for m in mine] == [(True, True)]               # nothing was written: still in the old group, no membership in the new one
    done = admin.post(url, params={"mode": "transfer"})
    assert done.status_code == 200 and done.json()["dry_run"] is False
    assert done.json()["carried"] == dry.json()["carried"]                                             # what the confirmation said is what travelled
    bulk = admin.post(f"/api/v1/auth/org/groups/{world.group.id}/students/bulk", params={"mode": "transfer", "dry_run": True}, json={"student_ids": [str(s0.id)]})
    assert bulk.status_code == 200 and bulk.json()["dry_run"] is True and bulk.json()["added"] == 1
    world.db.expire_all()
    assert world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=world.group.id, is_active=True).count() == 0     # the bulk dry run did not move him back


def test_r3_21_who_taught_defaults_to_the_substitute_of_that_date(world, now_4_nov):
    """A lesson of 28.10 had a substitute assigned (teacher2).  The list «Требует разбора» names HIM as the one who leads it (the default of «кто провёл»), and «Оформить» without ``taughtBy``
    gives the lesson to him - not to the teacher of the group."""
    from src.auth.schemas import StartAttendanceRequest
    from src.models.auth import LessonTeacherAssignment

    day, lid = lesson(date(2026, 10, 28))
    world.db.add(LessonTeacherAssignment(org_id=world.org.id, group_id=world.group.id, lesson_id=lid, lesson_date=day.isoformat(), teacher_id=world.teacher2.id, reason="Болезнь", set_by=world.owner.id))
    world.db.commit()
    (item,) = [i for i in world.svc.get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True) if i.lessonDate == day.isoformat() and i.lessonId == lid]
    assert item.teacherId == str(world.teacher2.id) and item.isSubstitution is True
    world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(world.owner.id), is_org_admin=True, money_right=True)
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one()
    assert row.teacher_id == world.teacher2.id


# ============================================================================================================ R3 m3: the pause
def _pause(world, who, membership, first, last, **extra):
    return make_client(world, user=who).post(f"/api/v1/auth/org/memberships/{membership}/pauses", json={"first_day": first, "last_day": last, **extra})


def test_r3_m3_a_pause_is_locked_by_the_teachers_of_THOSE_days_not_by_the_current_teacher_of_the_group(world, now_4_nov):
    """The group was taught by A until 20.10, now by B.  B's September is closed, A's is not: a pause on 14.09 is fine (B did not teach the group then); with A's September closed it is refused."""
    from src.models.auth import GroupAudit, StudentGroup
    from tests.pg_world import ensure_billing

    ensure_billing(world)
    a, b = world.teacher, world.teacher2
    world.db.add(GroupAudit(org_id=world.org.id, group_id=world.group.id, actor_id=world.owner.id, actor_role="owner", action="teacher_changed",
                            snapshot={"teacher_id": [str(a.id), str(b.id)]}, created_at=tashkent(2026, 10, 20)))
    world.group.teacher_id = b.id
    world.db.commit()
    sheet(world, b, date(2026, 9, 1), tashkent(2026, 10, 3))                           # B's September is closed (for other groups)
    membership = world.db.query(StudentGroup).filter_by(student_id=world.students[0].id, group_id=world.group.id).one().id
    ok = _pause(world, world.owner, membership, "2026-09-14", "2026-09-15")
    assert ok.status_code == 201, ok.text
    sheet(world, a, date(2026, 9, 1), tashkent(2026, 10, 3))                           # now A's September is closed too
    refused = _pause(world, world.owner, world.db.query(StudentGroup).filter_by(student_id=world.students[1].id, group_id=world.group.id).one().id, "2026-09-14", "2026-09-15")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "PAUSE_LOCKED"


def test_r3_m3_a_repeat_of_the_pause_is_a_repeat_even_if_a_lesson_began_meanwhile_and_the_sentence_names_a_forgotten_lesson(world, now_4_nov):
    from tests.payf_helpers import API as _unused  # noqa: F401
    from src.models.auth import StudentGroup
    from tests.pg_world import ensure_billing
    from tests.test_payroll_pg import lesson as lesson_row

    ensure_billing(world)
    membership = world.db.query(StudentGroup).filter_by(student_id=world.students[0].id, group_id=world.group.id).one().id
    first = _pause(world, world.owner, membership, "2026-11-10", "2026-11-12")
    assert first.status_code == 201, first.text
    lesson_row(world, date(2026, 11, 11), {world.students[1]: "present"}, status="in_progress")                 # a lesson inside the pause begins (a double click / a lost answer follows)
    again = _pause(world, world.owner, membership, "2026-11-10", "2026-11-12")
    assert again.status_code in (200, 201), again.text                                                          # a repeat, not a 409 (before: PauseLessonInProgress)
    other = world.db.query(StudentGroup).filter_by(student_id=world.students[2].id, group_id=world.group.id).one().id
    new = _pause(world, world.owner, other, "2026-11-10", "2026-11-12")
    assert new.status_code == 409 or new.status_code == 422, new.text
    assert "11.11" in new.json()["detail"]["message"] and "не завершён" in new.json()["detail"]["message"]       # not «сейчас идёт»: it began on the 11th and was never finished
