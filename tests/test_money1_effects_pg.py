"""Money-1 A3 / I-5: the answers of the actions that move money say what they did (and, for a dry run, what they WOULD do) to the packs of the students and to the saved sheets of the teachers
(PostgreSQL).  Numbers: ``area-payroll-devil.md`` ЗП-05 (saved 475 000, a late payment of S2 makes it 600 000), Д1 (a correction of a held lesson)."""
from datetime import date

from src.models.auth import AttendanceExcuse, StudentPayment

from src.auth import closed_period

from tests.money1_helpers import hold_lesson, lesson, now_4_nov, sheet, tashkent, reopen_now  # noqa: F401  (fixtures)
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
    body = {"amount": 300_000, "paid_on": _today().isoformat(), "method": "cash"}
    url = f"{API}/org/memberships/{mem2.id}/payments"
    dry = owner(world).post(url, params={"dry_run": True}, json=body)                       # R3 #15: the confirmation BEFORE the payment
    assert dry.status_code == 201 and dry.json()["dry_run"] is True
    assert world.db.query(StudentPayment).filter_by(membership_id=mem2.id).count() == 1     # nothing was written (the one payment of the world)
    (dry_line,) = [p for p in dry.json()["money_effects"]["payroll"] if p["month"] == m.label]
    assert (dry_line["freshTotal"], dry_line["changedBy"]) == (600_000, 125_000)             # R3 #22: the owner reads the signed sum BEFORE he confirms, not only in the toast after
    r = owner(world).post(url, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["dry_run"] is False
    effects = r.json()["money_effects"]
    (line,) = [p for p in effects["payroll"] if p["month"] == m.label]
    assert (line["savedTotal"], line["freshBefore"], line["freshAfter"], line["changedBy"], line["driftAfter"]) == (475_000, 475_000, 600_000, 125_000, 125_000)
    assert dry_line == line                                                                  # what the confirmation said is what the answer says
    assert line["state"] == "closed"                      # ДП-07: the sheet of the previous month was saved after it ended - the payment is NOT refused, it only says so
    assert "изменилась на +125 000 сум" in effects["text"] and "пересохраните" in effects["text"] and f", {closed_period.month_title(m.first)} " in effects["text"]       # the comma after the name stays
    # an administrator with the right hears that the sheet changes, without the sums of the teacher
    grant_money_right(world)
    paid_id = r.json()["payment"]["id"]
    voided = make_client(world, user=world.admin).post(f"{API}/org/payments/{paid_id}/void", json={"reason": "Внесли дважды"})
    assert voided.status_code == 200, voided.text
    admin_lines = voided.json()["money_effects"]["payroll"]
    assert [set(p) for p in admin_lines] == [{"month", "state"}]                             # the month only - and only because the pay REALLY moves
    assert "изменилась" in voided.json()["money_effects"]["text"]


def test_a_payment_that_moves_no_sheet_says_nothing_about_sheets(world, fixed_today):
    """R3 #21: «ведомость изменилась» only when the difference is not 0 - a payment of S1 (all paid already) for a month ahead moves nothing."""
    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    mem1 = student_group(world, s1)
    r = owner(world).post(f"{API}/org/memberships/{mem1.id}/payments", json={"amount": 100_000, "paid_on": _today().isoformat(), "method": "cash"})
    assert r.status_code == 201, r.text
    assert r.json()["money_effects"]["payroll"] == [] and r.json()["money_effects"]["text"] == ""


def test_r3_17_a_debt_of_september_paid_in_november_names_september(world, fixed_today):
    """R3-деньги НЕТОЧНОСТЬ-2: the old window «a month before the date of the payment .. now» saw only October; the debt of the second half of September (S2: 300 000 of 600 000 paid)
    paid on 20.11 moves the saved September sheet by +125 000 (5 lessons x 25 000) - the answer must name it."""
    from datetime import date as D

    fixed_today(D(2026, 10, 20))
    m, s1, s2 = g_pay_world(world)                          # September: S1 paid 600 000, S2 300 000
    assert m.label == "2026-09"
    sheet_ = save_sheet(world, m)
    assert sheet_["total_amount"] == 475_000
    fixed_today(D(2026, 11, 20))
    mem2 = student_group(world, s2)
    r = owner(world).post(f"{API}/org/memberships/{mem2.id}/payments", json={"amount": 300_000, "paid_on": "2026-11-20", "method": "cash"})
    assert r.status_code == 201, r.text
    lines = [p for p in r.json()["money_effects"]["payroll"] if p["month"] == "2026-09"]
    assert len(lines) == 1 and (lines[0]["savedTotal"], lines[0]["changedBy"]) == (475_000, 125_000)


def test_a_decision_on_an_excuse_says_it_before_and_after_what_it_does_to_the_sheet_and_the_pack(world, fixed_today):
    """Д1 numbers on the G-PAY world (50 %, 600 000, 25 000 per student per lesson): S1 is «excused» on the 13th (pending - it still counts), saved 475 000.  Confirming it takes the lesson off the pay
    of the teacher: -25 000, 450 000; taking the decision back: +25 000."""
    from datetime import datetime

    from sqlalchemy.orm.attributes import flag_modified

    from src.models.auth import GroupAttendance

    m, s1, s2 = g_pay_world(world)
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=m.d(13).isoformat()).one()
    row.records = {**row.records, str(s1.id): "excused"}
    flag_modified(row, "records")
    world.db.add(AttendanceExcuse(org_id=world.org.id, attendance_id=row.id, student_id=s1.id, reason="Болел, написал в Telegram", reason_by=world.admin.id,
                                  reason_at=datetime(2026, 9, 12), status="pending"))
    world.db.commit()
    assert save_sheet(world, m)["total_amount"] == 475_000
    excuse = world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id).one()
    url = f"/api/v1/auth/org/attendance/excuses/{excuse.id}/review"
    reopened = reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Справка пришла позже")
    assert reopened["changed"] is True
    client = make_client(world, user=world.owner)
    dry = client.post(url, params={"dry_run": True}, json={"decision": "confirmed"})
    assert dry.status_code == 200 and dry.json()["status"] == "pending"                         # nothing written
    effects = dry.json()["money_effects"]
    (line,) = effects["payroll"]
    assert (line["month"], line["freshBefore"], line["freshAfter"], line["changedBy"]) == (m.label, 475_000, 450_000, -25_000) and effects["dryRun"] is True and "изменится" in effects["text"]
    done = client.post(url, json={"decision": "confirmed"})
    assert done.status_code == 200 and done.json()["status"] == "confirmed" and done.json()["money_effects"]["dryRun"] is False
    (done_line,) = done.json()["money_effects"]["payroll"]
    assert (done_line["changedBy"], done_line["driftAfter"]) == (-25_000, -25_000)
    reset_dry = client.post(f"/api/v1/auth/org/attendance/excuses/{excuse.id}/reset", params={"dry_run": True})
    assert reset_dry.json()["status"] == "confirmed" and reset_dry.json()["money_effects"]["payroll"][0]["changedBy"] == 25_000       # taking the decision back pays the lesson again
    # R3 #21: the effect is THIS action's, not the drift that was there before: a late payment first (+125 000 drift), then the same confirmation still says -25 000
