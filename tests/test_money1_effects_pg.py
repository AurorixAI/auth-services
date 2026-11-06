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
    assert "изменилась на +125 000 сум" in effects["text"] and "сохраните новую сумму" in effects["text"] and f"зарплата за {closed_period.month_title(m.first)} " in effects["text"].lower()
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


def test_r3_20_an_advance_in_the_middle_of_the_month_is_not_settled_while_the_month_is_open(world, fixed_today):
    """ЗП-05 world: 475 000 saved; S2 pays 300 000 later -> 600 000.  The sheet was saved MID-month with an advance of 475 000 paid: the month is open, the data says 600 000."""
    from src.models.auth import PayrollSheet
    from tests.test_logicA_payroll_pg import payout

    m, s1, s2 = g_pay_world(world)
    saved = save_sheet(world, m)
    assert saved["total_amount"] == 475_000
    world.db.query(PayrollSheet).filter(PayrollSheet.id == saved["id"]).update({"updated_at": tashkent(m.first.year, m.first.month, 15)})        # saved on the 15th: the month is still open
    world.db.commit()
    first = payout(world, saved, 475_000)
    assert first["state"]["state"] == "settled"                                                    # the data says 475 000 so far
    owner(world).post(f"{API}/org/memberships/{student_group(world, s2).id}/payments", json={"amount": 300_000, "paid_on": _today().isoformat(), "method": "cash"})
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["payout"]
    assert (got["state"], got["basis"], got["target"], got["paid"], got["balance"]) == ("due", "fresh", 600_000, 475_000, 125_000)
    assert got["text"] == "Месяц открыт: начислено сейчас 600 000 сум, выплачено 475 000 сум, к выплате 125 000 сум"
    # paying more than the FRESH total is an overpay and needs the confirmation; the exact rest does not
    more = owner(world).post(f"{API}/org/payroll/sheets/{saved['id']}/payouts", json={"amount": 130_000, "paid_on": _today().isoformat(), "method": "cash"})
    assert more.status_code == 409 and more.json()["detail"]["code"] == "OVERPAY_CONFIRM_REQUIRED" and more.json()["detail"]["overpay"] == 5_000
    done = payout(world, saved, 125_000)
    assert (done["state"]["state"], done["state"]["balance"]) == ("settled", 0)
    # the next month gets no phantom advance: an open month is not frozen
    from src.auth.payroll_payouts import PayrollPayoutService

    assert PayrollPayoutService(world.db).carry_ins(str(world.org.id), [world.teacher.id], m.first.replace(day=28) + __import__("datetime").timedelta(days=10)) == {world.teacher.id: 0}
    # the month is CLOSED by a save after its end: the saved total is the target again (the A1b rule)
    world.db.query(PayrollSheet).filter(PayrollSheet.id == saved["id"]).update({"updated_at": tashkent(m.last.year, m.last.month, m.last.day) + __import__("datetime").timedelta(days=3)})
    world.db.commit()
    shut = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["payout"]
    assert (shut["basis"], shut["target"], shut["state"]) == ("saved", 475_000, "overpaid")


def test_r3_21_a_month_that_is_over_is_not_closed_over_an_unresolved_lesson_without_the_confirmation(world, fixed_today):
    """Saving the sheet of a month that ended with a lesson still going on (a returned one): ``confirm_open_lessons: false`` -> 409 with the list; ``true`` saves; an old client (no field) saves as before."""
    from tests.test_logicA_payroll_pg import LESSONS, fp
    from tests.test_payroll_pg import lesson as lesson_row

    m, s1, s2 = g_pay_world(world)
    free = next(n for n in range(1, 29) if n not in LESSONS)
    day = m.d(free)
    lesson_row(world, day, {s1: "present"}, status="in_progress")                    # a returned lesson: going on
    body = {"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": fp(world, m)}
    refused = owner(world).post(f"{API}/org/payroll/sheets", json={**body, "confirm_open_lessons": False})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "OPEN_LESSONS_CONFIRM_REQUIRED", refused.text
    detail = refused.json()["detail"]
    assert detail["count"] >= 1 and any(i["lessonDate"] == day.isoformat() and i["groupName"] for i in detail["items"])
    assert owner(world).post(f"{API}/org/payroll/sheets", json={**body, "confirm_open_lessons": True}).status_code == 201     # the owner said yes


def test_r3_n4_the_effects_of_many_sheets_of_one_month_calculate_the_month_once(world, now_4_nov, monkeypatch):
    """A rate backdated over a year names hundreds of sheets: the month is calculated ONCE for all its teachers, not once per sheet."""
    from src.auth import money_effects
    from src.auth.payroll_service import PayrollService
    from tests.pg_world import make_member, make_user

    sheets = [sheet(world, world.teacher, OCT, tashkent(2026, 10, 15))]
    for n in range(4):
        teacher = make_user(world.db, "teacher", f"Учитель{n}")
        make_member(world.db, world.org, teacher, "teacher")
        sheets.append(sheet(world, teacher, OCT, tashkent(2026, 10, 15)))
    calls = []
    real = PayrollService._collect
    monkeypatch.setattr(PayrollService, "_collect", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    probe = money_effects.SheetProbe(world.db, world.org.id, owner=True)
    probe.track(sheets)
    probe.measure("before")
    assert len(calls) == 1 and len(probe.before) == 5                                              # five sheets, one calculation of October


def test_r3_7_a_failure_of_the_numbers_after_the_commit_never_turns_a_written_payment_into_an_error(world, fixed_today, monkeypatch):
    """The payment is committed; the sentence about the sheets cannot be worked out: the answer is 201 with ``unavailable`` - not a 500 over money that is already written."""
    from src.auth import money_effects

    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    mem2 = student_group(world, s2)

    def broken(self, done):
        raise RuntimeError("the numbers broke")

    monkeypatch.setattr(money_effects.MembershipProbe, "block", broken)
    before = world.db.query(StudentPayment).filter_by(membership_id=mem2.id).count()
    r = owner(world).post(f"{API}/org/memberships/{mem2.id}/payments", json={"amount": 300_000, "paid_on": _today().isoformat(), "method": "cash"})
    assert r.status_code == 201, r.text
    assert r.json()["money_effects"] == {"students": [], "payroll": [], "text": "", "dryRun": False, "unavailable": True}
    world.db.expire_all()
    assert world.db.query(StudentPayment).filter_by(membership_id=mem2.id).count() == before + 1          # the payment is there, once
    monkeypatch.setattr(money_effects, "_block", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broke")))
    day, lid = lesson(m.d(13))
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Проверяем сбой расчёта")          # the month must be open for the impact of a correction
    r = owner(world).post(f"/api/v1/auth/org/groups/{world.group.id}/attendance/impact", json={"lessonDate": day.isoformat(), "lessonId": lid, "records": {}})
    assert r.status_code == 200 and r.json()["moneyEffects"]["unavailable"] is True                       # the same for the blocks of the lessons


def test_r3_12_a_save_of_the_sheet_that_waited_for_the_lock_is_stamped_after_the_reopening_it_waited_for(world, fixed_today):
    """A save that WAITS for the lock of the sheet while a reopening holds it: the reopening commits, the save goes on.  ``now`` is taken after the lock, so the save is newer than the reopening and closes
    the month again (before: ``now`` was taken before the wait - the month stayed «reopened» although the owner had just saved it)."""
    import threading
    import time
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth import business_clock as bc
    from src.auth.payroll_schemas import SheetCreate
    from src.auth.payroll_service import PayrollService
    from src.models.auth import PayrollPeriodReopening, PayrollSheet
    from tests.test_logicA_payroll_pg import fp

    m, s1, s2 = g_pay_world(world)
    saved = save_sheet(world, m)                                                                      # closed: saved after the month ended
    body = SheetCreate(month=m.label, teacher_id=world.teacher.id, revision=1, expected_fingerprint=fp(world, m))
    engine = world.db.get_bind()
    engine = getattr(engine, "engine", engine)
    holder = Session(engine)                                                                          # the "reopening": holds the sheet
    holder.execute(text("SELECT id FROM payroll_sheets WHERE id = :i FOR UPDATE"), {"i": saved["id"]})
    result: dict = {}

    def save():
        with Session(engine) as db:
            result["sheet"] = PayrollService(db).save_sheet(str(world.org.id), str(world.owner.id), body)

    thread = threading.Thread(target=save)
    thread.start()
    time.sleep(1.0)                                                                                   # the save is waiting for the lock now
    assert thread.is_alive()
    holder.add(PayrollPeriodReopening(org_id=world.org.id, teacher_id=world.teacher.id, period_month=m.first, sheet_id=saved["id"], sheet_revision=1, reopened_by=world.owner.id,
                                      reason="Правим ведомость", created_at=bc.utc_now()))
    holder.commit()                                                                                   # the reopening is committed, the lock is let go
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "sheet" in result
    world.db.expire_all()
    row = world.db.query(PayrollSheet).filter_by(id=saved["id"]).one()
    reopening = world.db.query(PayrollPeriodReopening).filter_by(org_id=world.org.id, teacher_id=world.teacher.id, period_month=m.first).one()
    assert row.revision == 2 and row.updated_at > reopening.created_at                                 # the save is NEWER than the reopening it waited for
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, m.first)["state"] == "closed"
