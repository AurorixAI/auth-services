"""LESSON-RULES С10.7: the REAL answers of the roads for the screens (rule 178), one agreed world with the human names of the owner's mock.  With ``LESSON_RULES_ANSWERS_DIR`` set the test writes every answer as
a file; always it checks the shape and the numbers it promises.

The world (today Thu 26.11.2026 12:00): the centre's owner «Рустам Валиев», the administrator WITHOUT the right of money «Каримова Д.», the teacher «Эргашев К.»; the group «Математика 7» (Mon / Wed
10:00-11:30, nine students, 450 000 each for November, the recalculation on: a lesson of the centre gives 450 000 / 9 = 50 000 to each, 9 x 50 000 = 450 000); the group «Физика 8» (Tue / Thu 14:00-15:30,
three students, no price) of the same teacher - for the entries of the history.  The teacher's reason: «Отключили свет».
By hand (the window of the centre is 30 days while the teacher acts, 7 at the end - the default):

    Математика 7, the teacher cancels («Отключили свет»), the owner decides:
      Mon 02.11 -> «Урок был» (restored), then the owner holds it: ``included_in_payroll``;     Mon 09.11 -> «Урок был», left to be marked: age 17 > 7, ``teacherCanMark`` false;
      Wed 11.11 -> «по вине центра» (``cancelled_centre``);   Mon 16.11 -> «праздник» (``cancelled_holiday``);   Wed 25.11 -> «Урок был»: age 1, ``teacherCanMark`` true;
      Wed 18.11 and Mon 23.11 WAIT (``cancel_waiting``): 8 and 3 days old; with the window 7 the teacher may withdraw the second only (``canWithdraw`` false / true).
    Физика 8 (the entries): Tue 03.11 held on 15.11 (12 days late), Tue 10.11 on 16.11 (6), Tue 17.11 on 19.11 (2) and corrected by the owner on 26.11; Thu 12.11 held by the administrator on 26.11 (14 days);
    a lesson added to the schedule by the teacher.
"""
import json
import os
from datetime import date, datetime
from pathlib import Path

from src.models.auth import MembershipPriceOverride

from tests.lesson_rules_helpers import MON, PAST_MON, WED, give_rate, marks_of, reconcile, save, save_sheet, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.ops_helpers import closed_sheet, grant_money_right, new_group, new_slot, new_student, seen_version

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
RULES = f"{API}/org/settings/lessons"
TUE, THU = "lesson-tue", "lesson-thu"
REASON = "Отключили свет"
STUDENTS = [("Абдуллаев", "Азиз"), ("Бекова", "Мадина"), ("Валиев", "Тимур"), ("Гафурова", "Лола"), ("Давронов", "Санжар"), ("Ерматова", "Нодира"), ("Жураев", "Сардор"), ("Исмоилова", "Зарина"), ("Каримов", "Умид")]


def people_named(w, group, people, names):
    for s, (last, first) in zip(people, names):
        s.last_name, s.first_name = last, first
    w.db.commit()


def agreed_world(pg_session, clock, **kw):
    """``(w, g, people, members)`` with the human names; the administrator has NO money right unless ``money_right``."""
    w, g, people, members = world(pg_session, clock, **kw)
    for user, last, first in ((w.A, "Эргашев", "Камол"), (w.owner, "Валиев", "Рустам"), (w.admin, "Каримова", "Дилноза")):
        user.last_name, user.first_name = last, first
    g.name = "Математика 7"
    people_named(w, g, people, STUDENTS)
    return w, g, people, members


def dump(out, name, r, code=200):
    assert r.status_code == code, (name, r.status_code, r.text)
    out[name] = r.json()
    return r.json()


def put_window(w, days):
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": days}).status_code == 200


def second_group(w, pg_session):
    g2 = new_group(w.db, w.org, w.ids, "GP", "Физика 8", w.A, [new_slot(TUE, 2, "14:00", "15:30", "2026-01-01"), new_slot(THU, 4, "14:00", "15:30", "2026-01-01")])
    people2 = [new_student(w.db, w.org, w.ids, f"p{i}", last, first, g2, joined=datetime(2026, 10, 20, 6, 0)) for i, (last, first) in enumerate((("Назаров", "Бекзод"), ("Одилова", "Шахноза"), ("Рахимов", "Жасур")))]
    w.db.commit()
    return g2, people2


def test_export_of_the_real_answers_for_the_screens(pg_session, clock):
    w, g, people, members = agreed_world(pg_session, clock)
    out = {}
    give_rate(w)
    g2, people2 = second_group(w, pg_session)
    put_window(w, 30)

    # ---- the entries of the history (the teacher holds the lessons of «Физика 8» late, on three days)
    for today, day in (("2026-11-15 12:00", "2026-11-03"), ("2026-11-16 12:00", "2026-11-10"), ("2026-11-19 12:00", "2026-11-17")):
        clock.set(today)
        reconcile(w, g2, day, TUE)
        save(w, g2, people2, day, TUE, marks=marks_of(people2, first_present=2))
    clock.set("2026-11-26 12:00")
    save(w, g2, people2, "2026-11-17", TUE, who="owner", marks=marks_of(people2, first_present=3), correctionReason="Ошибся в отметках")
    reconcile(w, g2, "2026-11-12", THU, who="owner")
    save(w, g2, people2, "2026-11-12", THU, who="owner", marks=marks_of(people2, first_present=2))
    body = {"groupId": str(g2.id), "dayOfWeek": 5, "startTime": "16:00", "endTime": "17:00", "validFrom": "2026-11-27"}
    assert client(w, "A").post(f"{API}/org/schedule/lessons", json=body).status_code in (200, 201)

    # ---- (С11 п.18) the extra lessons of the main group that the owner has decided: one paid, one not (GET /org/one-off-decisions/decided)
    for created, day, money, decision in (("2026-11-12 12:00", "2026-11-14", "wait", "pay"), ("2026-11-13 12:00", "2026-11-17", "wait", "skip")):
        clock.set(created)
        r = client(w, "A").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": day, "startTime": "12:00", "endTime": "13:00", "purpose": "extra", "money": money})
        assert r.status_code == 201, r.text
        extra_id = r.json()["id"]
        clock.set(f"{day} 15:00")
        reconcile(w, g, day, extra_id)
        save(w, g, people, day, extra_id, marks=marks_of(people, first_present=9))
        clock.set("2026-11-26 12:00")
        assert client(w, "owner").post(f"{API}/org/one-off-lessons/{extra_id}/money-decision", json={"decision": decision}).status_code == 200
    decided = dump(out, "one_off_decided", client(w, "owner").get(f"{API}/org/one-off-decisions/decided"))
    assert len(decided["items"]) == 2, decided

    # ---- (С11 п.18) the refusal EXCUSE_DECIDED for a student of the MAIN group: the teacher holds Wed 04.11 in progress, the administrator... the owner confirms the excuse of the first student, the teacher rewrites it
    clock.set("2026-11-26 12:00")
    reconcile(w, g, "2026-11-04", WED)
    marks9 = marks_of(people, mark="present")
    marks9[str(people[0].id)] = "excused"
    save(w, g, people, "2026-11-04", WED, status="in_progress", marks=marks9, excuseReasons={str(people[0].id): "Болел"})
    from src.models.auth import AttendanceExcuse

    w.db.expire_all()
    main_excuse = w.db.query(AttendanceExcuse).filter_by(student_id=people[0].id).one()
    assert client(w, "owner").post(f"{API}/org/attendance/excuses/{main_excuse.id}/review", json={"decision": "confirmed"}).status_code == 200
    r = save(w, g, people, "2026-11-04", WED, status="in_progress", marks={**marks9, str(people[0].id): "absent"}, expect=409)
    assert r.json()["detail"]["code"] == "EXCUSE_DECIDED" and r.json()["detail"]["studentName"] == "Абдуллаев Азиз", r.text
    excuse_main = r.json()

    # ---- the teacher cancels seven lessons of «Математика 7» («Отключили свет»); the answer of the first is the sentence after «Урока не было»
    for day, lesson in (("2026-11-02", MON), ("2026-11-09", MON), ("2026-11-11", WED), ("2026-11-16", MON), ("2026-11-18", WED), ("2026-11-23", MON), ("2026-11-25", WED)):
        r = teacher_cancels(w, g, day, lesson, reason=REASON)
        if day == "2026-11-23":
            dump(out, "cancel_teacher_waiting", r)
            assert r.json()["waiting"] is True and r.json()["consequence"] == "Отмену подтвердит администратор. За отменённый урок оплаты нет."
    restore = f"{API}/org/groups/{g.id}/lessons/restore"
    for day, lesson in (("2026-11-02", MON), ("2026-11-09", MON), ("2026-11-25", WED)):
        assert client(w, "owner").post(restore, json={"lessonDate": day, "lessonId": lesson, "reason": "Урок был"}).status_code == 200
    set_kind(w, g, "centre", day="2026-11-11", lesson=WED)
    set_kind(w, g, "holiday", day="2026-11-16", lesson=MON)
    reconcile(w, g, "2026-11-02", MON, who="owner")
    save(w, g, people, "2026-11-02", MON, who="owner", marks=marks_of(people, first_present=9))
    from tests.lesson_rules_helpers import kind_in_db

    assert kind_in_db(w, g, "2026-11-02", MON)[0] == "completed", kind_in_db(w, g, "2026-11-02", MON)
    marks_new = marks_of(people, first_present=5)
    dump(out, "attendance_impact", client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance/impact", json={"lessonDate": "2026-11-02", "lessonId": MON, "records": marks_new}))
    stale = save(w, g, people, "2026-11-02", MON, who="owner", marks=marks_new, correctionReason="Ошибся", expectedVersion=1, expect=409)
    assert stale.json()["detail"]["code"] == "ATTENDANCE_CONFLICT", stale.text
    out["attendance_conflict_save"] = stale.json()
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance/return", json={"lessonDate": "2026-11-02", "lessonId": MON, "reason": "Завершили по ошибке", "expectedVersion": 1})
    assert r.status_code == 409, r.text
    out["attendance_conflict_return"] = r.json()
    save_sheet(w, expect=201)                                                       # a draft of November (the month is not over): the return of a held lesson then names the sheet
    put_window(w, 7)

    # ---- the lists
    for who in ("owner", "admin"):
        pending = dump(out, f"pending_{who}", client(w, who).get(SUB, params={"status": "pending"}))
        assert [(i["source"], i["lesson"]["date"]) for i in pending["items"]] == [("unconfirmed", "2026-11-18"), ("unconfirmed", "2026-11-23")] or sorted(i["lesson"]["date"] for i in pending["items"]) == ["2026-11-18", "2026-11-23"]
        summary = dump(out, f"summary_{who}", client(w, who).get(f"{SUB}/summary"))
        assert summary["waitingTotal"] == (2 if who == "owner" else 0) and summary["unconfirmedTotal"] == 2 and pending["waitingTotal"] == summary["waitingTotal"]
    history = dump(out, "history", client(w, "owner").get(SUB, params={"status": "decided"}))
    kinds = [(i["kind"], i["lateDays"]) for i in history["items"] if i.get("source") == "audit"]
    assert sorted(k for k in kinds if k[0] == "backdated") == [("backdated", 2), ("backdated", 6), ("backdated", 12), ("backdated", 14), ("backdated", 24)], kinds
    assert {k[0] for k in kinds} == {"backdated", "corrected", "lesson_added"} and all(i["lesson"]["startTime"] for i in history["items"] if i.get("source") == "audit" and i["kind"] != "lesson_added")
    dump(out, "history_backdated_only", client(w, "owner").get(SUB, params={"status": "decided", "backdated_only": "true"}))
    mine = dump(out, "mine_teacher", client(w, "A").get(f"{SUB}/mine", params={"status": "all"}))
    by_day = {i["lesson"]["date"]: i for i in mine["items"] if i["status"] == "cancelled"}          # a given-back lesson also has the closed record of the return (origin system): «Урок возвращён», no marks
    assert {d: i["outcome"] for d, i in by_day.items()} == {"2026-11-02": "included_in_payroll", "2026-11-09": "lesson_restored", "2026-11-11": "cancelled_centre", "2026-11-16": "cancelled_holiday",
                                                           "2026-11-18": "cancel_waiting", "2026-11-23": "cancel_waiting", "2026-11-25": "lesson_restored"}
    assert (by_day["2026-11-09"]["teacherCanMark"], by_day["2026-11-25"]["teacherCanMark"]) == (False, True)
    assert (by_day["2026-11-18"]["canWithdraw"], by_day["2026-11-23"]["canWithdraw"]) == (False, True) and all(i["lesson"]["startTime"] == "10:00" and i["lesson"]["endTime"] == "11:30" for i in mine["items"] if i["lesson"]["lessonId"] in (MON, WED))
    confirmed_id = by_day["2026-11-11"]["id"]
    detail = dump(out, "detail_confirmed", client(w, "owner").get(f"{SUB}/{confirmed_id}"))
    assert detail["item"]["confirmedBy"]["name"] == "Валиев Р." and any(e["action"] == "confirmed" for e in detail["events"])
    opened = dump(out, "open_actions_teacher", client(w, "A").get(f"{API}/org/attendance/open-actions"))
    assert opened and all(a["windowDays"] == 7 and "ageDays" in a for a in opened)
    dump(out, "journal_teacher", client(w, "A").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"}))
    dump(out, "schedule_teacher", client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}))
    dump(out, "rules_lessons", client(w, "owner").get(RULES))

    # ---- the window of the decision: the ordinary one (9 x 50 000), the lesson older than the window, the holiday
    ordinary = dump(out, "dry_run_confirm_centre", set_kind(w, g, "centre", dry_run=True))
    assert (ordinary["students"], ordinary["total"], ordinary["restore_allowed"], ordinary["teacher_can_mark"]) == (9, 450_000, True, True), ordinary
    dump(out, "dry_run_confirm_holiday", set_kind(w, g, "holiday", dry_run=True))
    old = dump(out, "dry_run_confirm_old_lesson", set_kind(w, g, "centre", day="2026-11-18", lesson=WED, dry_run=True))
    assert old["teacher_can_mark"] is False and old["age_days"] == 8 and old["window_days"] == 7, old

    # ---- the dry run of the return of a held lesson (a sheet is named)
    url = f"{API}/org/groups/{g.id}/attendance/return"
    dump(out, "dry_run_return_payroll", client(w, "owner").post(url, params={"dry_run": "true"}, json={"lessonDate": "2026-11-02", "lessonId": MON, "reason": "Завершили по ошибке",
                                                                                                  "dryRun": True, **seen_version(w, g, "2026-11-02", MON)}))

    # ---- the refusals the screens show
    refusals = {}
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": "2026-11-18", "lessonId": WED})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LESSON_TOO_OLD" and r.json()["detail"]["windowDays"] == 7
    refusals["withdraw_too_old"] = r.json()
    r = client(w, "B").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": "2026-11-23", "lessonId": MON})
    assert r.status_code == 403, r.text
    refusals["withdraw_not_yours"] = r.json()
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-11-11", "lessonId": WED, "cancellationKind": "holiday", "expectedKind": "unconfirmed"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_DECIDED"
    refusals["already_decided"] = r.json()
    reconcile(w, g2, "2026-11-19", THU)                                             # Thu 19.11 (7 days: inside the window 7) was never started; all nine... three absent
    r = save(w, g2, people2, "2026-11-19", THU, expect=409)
    assert r.json()["detail"]["code"] == "NOBODY_CAME_BACKDATED" and r.json()["detail"]["message"] == "Никто не пришёл? Такой урок задним числом оформляет администратор — он видит его в уроках без отметки."
    refusals["nobody_came_backdated"] = r.json()
    refusals["excuse_decided"] = excuse_main          # the student of the MAIN group (taken above)
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-04", "lessonId": WED, "status": "in_progress", "records": {}})
    assert r.status_code in (403, 409), r.text
    refusals["lesson_too_old_save"] = r.json()
    refusals["policy_value_invalid"] = client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 5}).json()
    out["refusals"] = refusals

    # ---- the sheet of the month with the waiting cancellations (26.11: the warning; 02.12: the closing save is refused)
    calc = dump(out, "payroll_calculate_waiting", client(w, "owner").post(f"{API}/org/payroll/months/2026-11/calculate", json={"teacher_ids": [str(w.A.id)]}))
    warn = calc["sheets"][0]["warnings"]
    assert warn["unconfirmed_cancellations"] == 2 and len(warn["unconfirmed_items"]) == 2, warn
    clock.set("2026-12-02 12:00")
    r = save_sheet(w, expect=409, confirm_open_lessons=True)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "UNCONFIRMED_CANCELLATIONS" and r.json()["detail"]["count"] == 2, r.text
    out["payroll_gate_409"] = r.json()
    clock.set("2026-11-26 12:00")

    # ---- the other worlds of the window of the decision
    w2, g2w, p2, m2 = agreed_world(pg_session, clock)                              # different sums: two students have their own price
    for member, amount in ((m2[7], 360_000), (m2[8], 270_000)):
        w2.db.add(MembershipPriceOverride(membership_id=member.id, amount=amount, valid_from=date(2026, 11, 1), reason="скидка", created_at=datetime(2026, 11, 1)))
    w2.db.commit()
    teacher_cancels(w2, g2w, reason=REASON)
    sums = dump(out, "dry_run_confirm_different_sums", set_kind(w2, g2w, "centre", dry_run=True))
    # hand: seven students 50 000, the one at 360 000 gets 360 000 / 9 = 40 000, the one at 270 000 gets 270 000 / 9 = 30 000: 7 x 50 000 + 40 000 + 30 000 = 420 000
    assert sums["total"] == 420_000 and {d["amount"] for d in sums["deductions"]} == {30_000, 40_000, 50_000} and sums["students"] == 9, sums
    w3, g3, p3, m3 = agreed_world(pg_session, clock, recalc=False)                  # the recalculation is off
    teacher_cancels(w3, g3, reason=REASON)
    off = dump(out, "dry_run_confirm_recalc_off", set_kind(w3, g3, "centre", dry_run=True))
    assert off["recalc_off"] is True, off
    w4, g4, p4, m4 = agreed_world(pg_session, clock, students=0)                    # an archived group has no students (a group with balances cannot be archived)
    teacher_cancels(w4, g4, reason=REASON)
    assert client(w4, "owner").post(f"{API}/org/groups/{g4.id}/archive", json={"missedLessonsAction": "cancel", "reason": "Группа закрыта"}).status_code == 200
    archived = dump(out, "dry_run_confirm_archived", set_kind(w4, g4, "centre", dry_run=True))
    assert archived["restore_allowed"] is False, archived

    # ---- (С11 п.18 / п.14) the confirmation in a CLOSED month: 409 PERIOD_CLOSED
    w5, g5, p5, m5 = agreed_world(pg_session, clock)
    teacher_cancels(w5, g5, reason=REASON)
    closed_sheet(w5, w5.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    r = set_kind(w5, g5, "centre", expect=409)
    assert r.json()["detail"]["code"] == "PERIOD_CLOSED", r.text
    out["confirm_period_closed_409"] = r.json()

    # ---- the zero of the dry run: the three words of the server
    wz, gz, pz, mz = agreed_world(pg_session, clock, period_mode="lessons", lessons_per_payment=8, billing_anchor="payment_date")
    teacher_cancels(wz, gz, reason=REASON)
    assert dump(out, "dry_run_zero_packages", set_kind(wz, gz, "centre", dry_run=True))["zero_reason"] == "packages"
    wn, gn, pn, mn = agreed_world(pg_session, clock, students=0)
    new_student(wn.db, wn.org, wn.ids, "late1", "Новичков", "Тимур", gn, joined=datetime(2026, 11, 24, 6, 0))
    wn.db.commit()
    teacher_cancels(wn, gn, reason=REASON)
    assert dump(out, "dry_run_zero_nobody_charged", set_kind(wn, gn, "centre", dry_run=True))["zero_reason"] == "nobody_charged"
    wo, go, po, mo = agreed_world(pg_session, clock)
    clock.set("2026-11-24 12:00")
    r = client(wo, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(go.id), "lessonDate": "2026-11-25", "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
    clock.set("2026-11-26 12:00")
    teacher_cancels(wo, go, day="2026-11-25", lesson=r.json()["id"], reason=REASON)
    assert dump(out, "dry_run_zero_one_off", set_kind(wo, go, "centre", day="2026-11-25", lesson=r.json()["id"], dry_run=True))["zero_reason"] == "one_off"

    # ---- «Ждут решения» of the administrator who is the AUTHOR of a waiting cancellation: ``canWithdraw`` true for him
    wa, ga, pa, ma = agreed_world(pg_session, clock)
    clock.set("2026-11-22 12:00")
    r = client(wa, "A").post(SUB, json={"lessons": [{"groupId": str(ga.id), "lessonId": MON, "lessonDate": PAST_MON}], "reasonCode": "sick"})
    rid = r.json()["created"][0]["id"]
    clock.set("2026-11-26 12:00")
    assert client(wa, "admin").post(f"{SUB}/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"}).status_code == 200
    pend = dump(out, "pending_admin_author", client(wa, "admin").get(SUB, params={"status": "pending"}))
    assert [i["canWithdraw"] for i in pend["items"] if i["source"] == "unconfirmed"] == [True]

    # ---- the history: an evening lesson started in time and closed after midnight is NOT an entry
    we, ge, pe, me = agreed_world(pg_session, clock)
    gev = new_group(we.db, we.org, we.ids, "GV", "Вечерняя физика", we.A, [new_slot("lesson-eve", 3, "21:00", "22:00", "2026-01-01")])
    sev = new_student(we.db, we.org, we.ids, "ev1", "Вечеров", "Артур", gev)
    we.db.commit()
    clock.set("2026-11-25 21:05")
    assert client(we, "A").post(f"{API}/org/groups/{gev.id}/attendance/start", json={"lessonDate": "2026-11-25", "lessonId": "lesson-eve"}).status_code == 200
    clock.set("2026-11-26 00:10")
    save(we, gev, [sev], "2026-11-25", "lesson-eve", marks={str(sev.id): "present"})
    clock.set("2026-11-26 12:00")
    ev = dump(out, "history_evening_not_late", client(we, "owner").get(SUB, params={"status": "decided"}))
    assert [i for i in ev["items"] if i.get("source") == "audit"] == []

    # ---- the schedule with the waiting cancellations as the OWNER reads it
    dump(out, "schedule_owner", client(w, "owner").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}))

    # ---- a closed month in the journal and the schedule of the teacher (October of the teacher A is closed on 02.11)
    closed_sheet(w, w.A, date(2026, 10, 1), saved="2026-11-02 12:00")
    jm = dump(out, "journal_teacher_month_closed", client(w, "A").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-10"}))
    assert any(l.get("monthClosed") is True for l in jm["lessons"]), "no lesson of the closed month in the journal"
    sm = dump(out, "schedule_teacher_month_closed", client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-01", "date_to": "2026-10-31"}))
    assert any(i.get("monthClosed") is True for i in sm)

    # ---- (С13.8) the new answers: the preview after somebody else's decision (409), the cancellation of the administrator without the right (``waiting``), the preview of the cancellation with the setting off
    w6, g6, p6, m6 = agreed_world(pg_session, clock)
    teacher_cancels(w6, g6, reason=REASON)
    set_kind(w6, g6, "centre")
    late = dump(out, "dry_run_confirm_after_other_decided_409", set_kind(w6, g6, "centre", dry_run=True, expect=409), 409)
    assert late["detail"]["code"] == "ALREADY_DECIDED" and late["detail"]["kind"] == "centre", late
    w7, g7, p7, m7 = agreed_world(pg_session, clock)
    ask = f"{API}/org/groups/{g7.id}/lessons/cancel-consequence"
    cc = dump(out, "cancel_consequence_waiting_admin", client(w7, "admin").get(ask, params={"lessonDate": PAST_MON, "lessonId": MON}))
    assert cc["waiting"] is True, cc
    adm = dump(out, "cancel_admin_without_right_waiting", teacher_cancels(w7, g7, who="admin", reason=REASON))
    assert adm["waiting"] is True and adm["consequence"].startswith("Отмену подтвердит владелец"), adm
    w8, g8, p8, m8 = agreed_world(pg_session, clock)
    assert client(w8, "owner").put(RULES, json={"past_cancel_needs_confirmation": False}).status_code == 200
    cc2 = dump(out, "cancel_consequence_setting_off_admin", client(w8, "admin").get(f"{API}/org/groups/{g8.id}/lessons/cancel-consequence", params={"lessonDate": PAST_MON, "lessonId": MON}))
    assert cc2["waiting"] is False, cc2
    jt = out["journal_teacher"]["lessons"]
    assert [(l["date"], l["waiting"], l["canWithdraw"], l["reportedByRole"]) for l in jt if l["date"] == "2026-11-23"] == [("2026-11-23", True, True, "teacher")]
    kinds = {i["kind"]: i["canReturn"] for i in out["history"]["items"] if i.get("source") == "audit"}
    assert kinds.get("backdated") is True and kinds.get("lesson_added") is None, kinds          # the owner reads: a held lesson of an open month may be returned; the entry of an added lesson has no such question

    # ---- the history entry of an administrator WITH the right of money (the owner gives the right at the end: every answer above is the administrator without it)
    grant_money_right(w)
    reconcile(w, g2, "2026-11-05", THU, who="admin")
    save(w, g2, people2, "2026-11-05", THU, who="admin", marks=marks_of(people2, first_present=2))
    hist = dump(out, "history_admin_with_right", client(w, "owner").get(SUB, params={"status": "decided"}))
    assert [(i["by"]["role"], i["lateDays"]) for i in hist["items"] if i.get("source") == "audit" and i["lesson"]["date"] == "2026-11-05"] == [("admin", 21)]

    target = os.environ.get("LESSON_RULES_ANSWERS_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        for name, body in out.items():
            Path(target, f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
