"""Money-1 A3 / I-5: the answers of the actions that move money say what they did (and, for a dry run, what they WOULD do) to the packs of the students and to the saved sheets of the teachers
(PostgreSQL).  Numbers: ``area-payroll-devil.md`` ЗП-05 (saved 475 000, a late payment of S2 makes it 600 000), Д1 (a correction of a held lesson)."""
from datetime import date

from src.models.auth import AttendanceExcuse

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, save, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, _today, g_pay_world, owner, save_sheet, student_group  # noqa: F401
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)

pytestmark = needs_pg
OCT = date(2026, 10, 1)


def test_a_late_payment_names_the_sheet_it_moves_with_the_sum_ЗП05(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    sheet_ = save_sheet(world, m)
    assert sheet_["total_amount"] == 475_000
    mem2 = student_group(world, s2)
    r = owner(world).post(f"{API}/org/memberships/{mem2.id}/payments", json={"amount": 300_000, "paid_on": _today().isoformat(), "method": "cash"})
    assert r.status_code == 201, r.text
    effects = r.json()["money_effects"]
    line = next(p for p in effects["payroll"] if p["month"] == m.label)
    assert (line["savedTotal"], line["freshTotal"], line["changedBy"]) == (475_000, 600_000, 125_000)
    assert line["state"] == "closed"                      # ДП-07: the sheet of the previous month was saved after it ended - the payment is NOT refused, it only says so
    assert "+125 000" in effects["text"] and "пересохраните" in effects["text"]
    # an administrator with the right hears that the sheet changes, without the sums of the teacher
    grant_money_right(world)
    paid_id = r.json()["payment"]["id"]
    voided = make_client(world, user=world.admin).post(f"{API}/org/payments/{paid_id}/void", json={"reason": "Внесли дважды"})
    assert voided.status_code == 200, voided.text
    assert all(set(p) == {"month", "state"} for p in voided.json()["money_effects"]["payroll"])


def test_a_decision_on_an_excuse_says_it_before_and_after_what_it_does_to_the_pack_and_the_sheet(world, now_4_nov):
    day, lid = lesson(date(2026, 10, 28))
    from src.auth.schemas import StartAttendanceRequest

    world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(world.admin.id), is_org_admin=True)
    s1, s2, s3 = world.students
    save(world, day, lid, [(s1, "excused"), (s2, "present"), (s3, "present")], status="completed", excuseReasons={s1.id: "Болел, написал в Telegram"})
    sheet(world, world.teacher, OCT, tashkent(2026, 10, 30), total=1_000_000)
    excuse = world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id).one()
    url = f"/api/v1/auth/org/attendance/excuses/{excuse.id}/review"
    client = make_client(world, user=world.owner)
    dry = client.post(url, params={"dry_run": True}, json={"decision": "confirmed"})
    assert dry.status_code == 200 and dry.json()["status"] == "pending"                         # nothing written
    effects = dry.json()["money_effects"]
    assert effects["students"] == [{"studentId": str(s1.id), "delta": 1, "name": effects["students"][0]["name"]}]     # the lesson goes back into the pack of S1
    assert effects["payroll"][0]["month"] == "2026-10" and effects["dryRun"] is True and "изменится" in effects["text"]
    done = client.post(url, json={"decision": "confirmed"})
    assert done.status_code == 200 and done.json()["status"] == "confirmed" and done.json()["money_effects"]["dryRun"] is False
    assert done.json()["money_effects"]["students"][0]["delta"] == 1
    reset_dry = client.post(f"/api/v1/auth/org/attendance/excuses/{excuse.id}/reset", params={"dry_run": True})
    assert reset_dry.json()["status"] == "confirmed" and reset_dry.json()["money_effects"]["students"][0]["delta"] == -1       # taking the decision back spends the lesson again
