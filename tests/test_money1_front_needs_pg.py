"""Money-1 R3 #22-#28: what the screens of package A asked of the server - the sum BEFORE the confirmation (``freshTotal``), the dry run of «Вернуть в разбор», the impact for the teacher of his own
lesson, the mark of a returned lesson in the journal, the name of the group in the open lessons, the list of the decided additional lessons (PostgreSQL)."""
from datetime import date

import pytest

from src.auth import money_effects as me
from src.models.auth import GroupAttendance

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import ensure_billing, make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth/org/groups"


def cancel_by_centre(w, day):
    from src.auth.schemas import ReconcileMissedLessonRequest

    day, lid = lesson(day)
    w.svc.cancel_missed_group_attendance(str(w.org.id), str(w.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Нет света", cancellationKind="centre"),
                                         cancelled_by=str(w.admin.id), is_org_admin=True, is_org_manager=True)
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
    assert f"{total:,}".replace(",", " ") in out["moneyEffects"]["text"] and "исчезнет вычет" in out["moneyEffects"]["text"]
    world.db.expire_all()
    assert world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one().status == "cancelled"        # nothing written
    assert me.centre_deductions(world.db, world.org.id, world.group.id) == before
    done = owner.post(url, json=body).json()
    assert done["dryRun"] is False and done["moneyEffects"]["dryRun"] is False and "исчез вычет" in done["moneyEffects"]["text"]
    assert done["deductions"] == out["deductions"]                                                  # what the confirmation said is what happened
    assert sum(me.centre_deductions(world.db, world.org.id, world.group.id).values()) == 0


def test_r3_24_the_teacher_of_a_held_lesson_asks_the_impact_of_his_own_correction_without_sums_nobody_else_without_the_right(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    s1, s2, s3 = world.students
    sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 10, 30))
    url = f"{API}/{world.group.id}/attendance/impact"
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "records": {str(s1.id): "absent", str(s2.id): "present", str(s3.id): "present"}}
    mine = make_client(world, user=world.teacher).post(url, json=body)
    assert mine.status_code == 200, mine.text
    effects = mine.json()["moneyEffects"]
    assert effects["dryRun"] is True and all(set(line) <= {"month", "state"} for line in effects["payroll"])      # the month and the state only: no sum of anybody's pay
    other = make_client(world, user=world.teacher2).post(url, json=body)                                       # another teacher, not the one of this lesson
    assert other.status_code == 403 and other.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert make_client(world, user=world.admin).post(url, json=body).status_code == 403                         # an administrator without the money right
    grant_money_right(world)
    assert make_client(world, user=world.admin).post(url, json=body).status_code == 200
    assert make_client(world, user=world.owner).post(url, json=body).status_code == 200


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
