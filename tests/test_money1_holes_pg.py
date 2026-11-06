"""Money-1 pack A: one test for each hole of the analyses, with the NUMBERS of the analyses (PostgreSQL).  Each fails on the code before the pack (the mutations are in the report).

* the chain ЗП-04 / Д1 / ЗПД-2: a paid sheet of October, the month closes, a mistake is found: refused -> reopened (journal) -> the lesson returned -> the sheet says «изменилось на −50 000» ->
  the sheet saved again = the month closes again, the payout by the SAVED total never moved;
* П1: «Урока не было» by a slip: 5 students x 46 000 = 230 000 of deduction, gone with the return;
* ОШ-1 (T25): a pause while a lesson of the group goes on; П7 (T31): the check of unfinished lessons fails - «не удалось проверить», not 0; Д15: the human texts;
* I-7: two at once = one record."""
from datetime import date

import pytest

from src.auth import closed_period
from src.auth.lesson_return import ReturnLessonRequest, return_completed_lesson
from src.auth.service import AuthService
from src.models.auth import GroupAttendance, GroupAttendanceAudit, PayrollPeriodReopening, StudentGroup

from tests.money1_helpers import hold_lesson, now_4_nov, sheet, tashkent, reopen_now  # noqa: F401  (fixtures)
from tests.ops_helpers import clock, closed_sheet, grant_money_right, run_parallel  # noqa: F401  (fixtures)
from tests.pg_world import LIVE_SLOT, business_today, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, payout, save_sheet  # noqa: F401
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)
from tests.test_payroll_pg import previous_month

pytestmark = needs_pg
RET = "/api/v1/auth/org/groups/{gid}/attendance/return"


def test_the_chain_a_paid_sheet_closes_the_month_a_mistake_is_reopened_returned_and_the_payout_never_moves(world):
    m, s1, s2 = g_pay_world(world)
    sheet_ = save_sheet(world, m)
    assert sheet_["total_amount"] == 475_000                                                       # 300 000 (S1, 12 lessons) + 175 000 (S2, 7 lessons through the 15th)
    first = payout(world, sheet_, 475_000)
    assert first["state"]["state"] == "settled"
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, m.first)["state"] == "closed"        # saved after the month ended
    day = m.d(13)                                                                                   # a lesson both students are covered for
    body = {"lessonDate": day.isoformat(), "lessonId": f"s{day.isoweekday()}", "reason": "Завершили по ошибке", "expectedVersion": 1}        # the version the screen saw (R3 #11)
    url = RET.format(gid=world.group.id)
    shut = owner(world).post(url, json=body)
    assert shut.status_code == 409 and shut.json()["detail"]["code"] == "PERIOD_CLOSED" and shut.json()["detail"]["month"] == m.label
    reopened = owner(world).post(f"{API}/org/payroll-periods/{m.label}/reopen", json={"teacher_id": str(world.teacher.id), "reason": "Урок 13-го завершили по ошибке", "sheet_revision": 1})
    assert reopened.status_code == 200 and reopened.json()["changed"] is True and reopened.json()["state"] == "reopened"
    dry = owner(world).post(url, json={**body, "dryRun": True}).json()["moneyEffects"]
    assert dry["students"] == [] and dry["payroll"][0]["state"] == "reopened" and dry["dryRun"] is True      # calendar payment: no pack; the sheet is named before the confirmation
    done = owner(world).post(url, json=body)
    assert done.status_code == 200 and done.json()["status"] == "in_progress"
    line = done.json()["moneyEffects"]["payroll"][0]
    assert (line["savedTotal"], line["freshBefore"], line["freshAfter"], line["changedBy"]) == (475_000, 475_000, 425_000, -50_000)       # two covered students x 25 000 gone from the pay
    state = owner(world).get(f"{API}/org/payroll/sheets/{sheet_['id']}").json()["payout"]
    assert (state["state"], state["basis"], state["balance"], state["changed_by"]) == ("overpaid", "fresh", -50_000, -50_000)       # R3 #20: the month is REOPENED (open) - the target is what the data says now, 425 000
    assert state["text"] == ("Месяц открыт: начислено сейчас 425 000 сум, выплачено 475 000 сум. Выплачено вперёд 50 000 сум: "
                            "покроется начислениями до конца месяца; если останется — уменьшит следующую выплату")
    assert "−50 000 сум" in state["change_text"] and "Сохраните новую сумму" in state["change_text"]
    fp = owner(world).get(f"{API}/org/payroll/sheets/{sheet_['id']}/diff").json()["source_fingerprint"]
    resaved = owner(world).patch(f"{API}/org/payroll/sheets/{sheet_['id']}", json={"revision": 1, "recalculate": True, "expected_fingerprint": fp})
    assert resaved.status_code == 200 and resaved.json()["total_amount"] == 425_000
    after = resaved.json()["payout"]
    assert (after["state"], after["balance"], after["advance_out"], after["changed_by"]) == ("overpaid", -50_000, 50_000, 0)    # 475 000 paid against 425 000: an advance, told once
    world.db.expire_all()
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, m.first)["state"] == "closed"        # the save after the correction closes the month again
    audit = world.db.query(GroupAttendanceAudit).filter_by(org_id=world.org.id, action="completed_reverted").one()
    assert audit.correction_reason == "Завершили по ошибке" and world.db.query(PayrollPeriodReopening).filter_by(org_id=world.org.id).one().reason.startswith("Урок 13-го")


def test_p1_a_lesson_cancelled_by_a_slip_costs_230_000_of_deduction_until_it_is_returned_to_review(pg_session, clock):
    from tests.payf_helpers import API as PAPI, client
    from tests.test_p154_money_pg import build_org, deducted, five

    w = build_org(pg_session)
    g, students, ms = five(w, clock)                                                               # 26.10 cancelled by the centre: 46 000 from each of 5
    assert [deducted(w, mm) for mm in ms] == [46_000] * 5 and sum(deducted(w, mm) for mm in ms) == 230_000
    clock.set("2026-10-26 12:00")                                                                  # the lesson (10:00-11:00) has begun: it used to be un-restorable for everybody
    body = {"lessonDate": "2026-10-26", "lessonId": "lesson-mon", "reason": "Урок был, отменили по ошибке"}
    grant = client(w, "owner")
    from src.models.auth import Organization

    org = pg_session.query(Organization).filter_by(id=w.org.id).one()
    settings = dict(org.settings or {})
    settings["payments"] = {**(settings.get("payments") or {}), "admin_can_manage": False}
    org.settings = settings
    pg_session.commit()
    denied = client(w, "admin").post(f"{PAPI}/org/groups/{g.id}/lessons/restore", json=body)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "RESTORE_PAST" and "с доступом к деньгам" in denied.json()["detail"]["message"]
    ok = grant.post(f"{PAPI}/org/groups/{g.id}/lessons/restore", json=body)
    assert ok.status_code == 200 and ok.json()["returnedToReview"] is True and "перерасчёт" in ok.json()["moneyEffects"]["text"]
    assert [deducted(w, mm) for mm in ms] == [0] * 5                                               # the 230 000 are gone with the cancelled row
    missed = [a for a in client(w, "admin").get(f"{PAPI}/org/attendance/open-actions").json() if a["lessonId"] == "lesson-mon" and a["lessonDate"] == "2026-10-26"]
    assert len(missed) == 1 and missed[0]["actionType"] == "missed"                                # and the teacher can hold it again


def test_osh1_t25_a_pause_is_refused_while_a_lesson_of_the_group_goes_on(world):
    from src.auth.schemas import StartAttendanceRequest

    today = business_today()
    m = world.db.query(StudentGroup).filter_by(student_id=world.students[0].id).one()
    url = f"/api/v1/auth/org/memberships/{m.id}/pauses"
    body = {"first_day": today.isoformat(), "last_day": today.isoformat(), "reason": "Болезнь"}
    world.svc.start_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=today, lessonId=LIVE_SLOT), started_by=str(world.teacher.id), is_org_admin=False)
    refused = make_client(world, user=world.owner).post(url, json=body)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "LESSON_IN_PROGRESS" and "после его завершения" in refused.json()["detail"]["message"]
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id).update({"status": "cancelled"})
    world.db.commit()
    assert make_client(world, user=world.owner).post(url, json=body).status_code == 201


def test_p7_t31_when_the_check_of_unfinished_lessons_fails_the_month_says_unknown_not_zero(world, monkeypatch):
    m, s1, s2 = g_pay_world(world)
    ok = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"][0]["warnings"]
    assert ok["open_lessons"] == 0 and ok["open_lessons_unknown"] is False

    def boom(self, *a, **kw):
        raise RuntimeError("schedule is broken")

    monkeypatch.setattr(AuthService, "get_open_attendance_actions", boom)
    bad = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"][0]["warnings"]
    assert bad["open_lessons"] is None and bad["open_lessons_unknown"] is True


def test_d15_the_refusals_of_a_held_and_of_a_cancelled_lesson_say_who_can_do_what(world, now_4_nov):
    from tests.pg_world import save as save_marks
    from src.auth.schemas import ReconcileMissedLessonRequest
    from tests.money1_helpers import lesson

    day, lid = hold_lesson(world, date(2026, 10, 30))
    with pytest.raises(ValueError, match="Вернуть урок"):
        save_marks(world, day, lid, [(s, "present") for s in world.students], status="in_progress", correctionReason="Хочу вернуть")
    cday, clid = lesson(date(2026, 10, 29))
    world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id), ReconcileMissedLessonRequest(lessonDate=cday, lessonId=clid, cancellationReason="Нажали по ошибке"),
                                             cancelled_by=str(world.admin.id), is_org_admin=True, is_org_manager=True)
    with pytest.raises(ValueError, match="Вернуть на отметку"):
        save_marks(world, cday, clid, [(s, "present") for s in world.students], status="in_progress")


def test_i7_two_returns_at_once_leave_one_record_and_two_reopenings_at_once_one_journal_row(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 11, 2))
    req = ReturnLessonRequest(lessonDate=day, lessonId=lid, reason="Завершили по ошибке", expectedVersion=int(world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat(), lesson_id=lid).one().version))
    grant_money_right(world)
    first = run_parallel(world, lambda svc: reopen_now(svc.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Исправляем ошибку"),
                         lambda svc: reopen_now(svc.db, world.org.id, world.teacher.id, "2026-10", world.admin.id, "Исправляем ошибку"))
    assert [e for _, e in first if e is not None] == [] and sorted(r["changed"] for r, _ in first) == [False, True]
    world.db.expire_all()
    assert world.db.query(PayrollPeriodReopening).filter_by(org_id=world.org.id).count() == 1
    second = run_parallel(world, lambda svc: return_completed_lesson(svc.db, world.org.id, world.group.id, req, actor_id=world.owner.id, owner=True),
                          lambda svc: return_completed_lesson(svc.db, world.org.id, world.group.id, req, actor_id=world.admin.id, owner=False))
    assert [e for _, e in second if e is not None] == [] and sorted(r["changed"] for r, _ in second) == [False, True]
    world.db.expire_all()
    assert world.db.query(GroupAttendanceAudit).filter_by(org_id=world.org.id, action="completed_reverted").count() == 1


def test_zp04_a_rate_changed_backwards_says_which_saved_sheets_it_moves_and_the_payout_does_not_move(world):
    """ЗП-04: the rate 50 % -> 60 % from the month: the saved 475 000 becomes 570 000 (+95 000); the payout of 475 000 stays settled by the SAVED total."""
    m, s1, s2 = g_pay_world(world)
    sheet_ = save_sheet(world, m)
    payout(world, sheet_, 475_000)
    r = owner(world).put(f"{API}/org/payroll/rates/{world.teacher.id}", json={"valid_from": m.label, "percents": {"group": "60"}})
    assert r.status_code == 200, r.text
    effects = r.json()["money_effects"]
    (line,) = [p for p in effects["payroll"] if p["month"] == m.label]
    assert (line["savedTotal"], line["freshBefore"], line["freshAfter"], line["changedBy"]) == (475_000, 475_000, 570_000, 95_000) and "+95 000" in effects["text"]
    state = owner(world).get(f"{API}/org/payroll/sheets/{sheet_['id']}").json()["payout"]
    assert (state["state"], state["balance"], state["changed_by"]) == ("settled", 0, 95_000)


def test_zp04_a_divisor_changed_backwards_names_the_sheets_with_the_sum(world):
    m, s1, s2 = g_pay_world(world)
    sheet_ = save_sheet(world, m)
    r = owner(world).put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": m.label})
    assert r.status_code == 200, r.text
    (line,) = [p for p in r.json()["money_effects"]["payroll"] if p["month"] == m.label]
    assert line["savedTotal"] == 475_000 and line["changedBy"] < 0 and line["freshAfter"] == 475_000 + line["changedBy"] and line["freshBefore"] == 475_000
    back = owner(world).put(f"{API}/org/payroll/settings", json={"divisor": 12, "valid_from": m.label}).json()["money_effects"]["payroll"]
    assert len(back) == 1 and back[0]["changedBy"] == -line["changedBy"] and back[0]["driftAfter"] == 0         # back to 12: THIS change undoes the first one, the sheet equals the saved again (R3 #21)


def test_i4_the_month_names_its_unfinished_lessons_before_the_owner_saves_it(world):
    from tests.test_payroll_pg import lesson as held

    m, s1, s2 = g_pay_world(world)
    held(world, m.d(28), {}, status="in_progress")                                                 # a lesson of the month that nobody closed (the 28th: every month has it - LESSONS 76/102, a February has no 29th)
    r = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={"teacher_ids": [str(world.teacher.id)]})
    warn = r.json()["sheets"][0]["warnings"]
    assert warn["open_lessons"] == 1 and warn["open_lesson_items"][0]["lessonDate"] == m.d(28).isoformat()
    overview = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"][0]["warnings"]
    assert overview["open_lessons"] == 1 and overview["open_lesson_items"] == [{"lessonDate": m.d(28).isoformat(), "groupId": str(world.group.id), "groupName": world.group.name, "lessonId": f"s{m.d(28).isoweekday()}", "kind": "in_progress",
                                                                                          "teacherId": str(world.teacher.id)}]
