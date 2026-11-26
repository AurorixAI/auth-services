"""LESSON-RULES С11 (п.4-16 and the tests that were missing): the refusals, the one rule of «Отозвать», the words of the teacher's block, the history entries, the zero of the dry run, the races
«отзыв против подтверждения» in both orders, the confirmation in another billing mode / in the next month, the make-up.

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.», Mon / Wed 10:00-11:30, nine students who paid 450 000 for November; one lesson «по вине центра» = 450 000 / 9 = 50 000 each).
Today Thu 26.11.2026 12:00 unless a test sets another time.  The expectation of each test is in its comment BEFORE the call."""
import logging
import threading
from datetime import datetime

import pytest
from sqlalchemy import text

from src.models.auth import GroupAttendance

from tests.lesson_rules_helpers import MON, PAST_MON, WED, account, accounts, kind_in_db, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.ops_helpers import closed_sheet, grant_money_right, new_group, new_slot, new_student
from tests.payf_helpers import D, cancel, statement
from tests.test_lesson_rules_c11_settings_race_pg import _waiting, own_session_client

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
RULES = f"{API}/org/settings/lessons"
NINE_X_50 = [(50_000, 0, 50_000)] * 9


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def mine(w, who="A"):
    r = client(w, who).get(f"{SUB}/mine", params={"status": "all"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def withdraw(w, g, who="A", day=PAST_MON, lesson=MON, expect=200, **extra):
    r = client(w, who).post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": day, "lessonId": lesson, **extra})
    assert r.status_code == expect, r.text
    return r


def admin_cancels_past_by_decision(w, g, clock, day=PAST_MON, lesson=MON):
    """A asks on 22.11 for ``day``; on 26.11 the administrator WITHOUT the right decides «Отменить урок»: the cancellation WAITS (``unconfirmed``), its author is the administrator."""
    clock.set("2026-11-22 12:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": lesson, "lessonDate": day}], "reasonCode": "sick"})
    assert r.status_code == 201, r.text
    rid = r.json()["created"][0]["id"]
    clock.set("2026-11-26 12:00")
    d = client(w, "admin").post(f"{SUB}/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"})
    assert d.status_code == 200 and d.json()["result"]["cancellationKind"] == "unconfirmed", d.text
    return rid


# ============================================================================================ п.4 only the refusals of the rules are caught
def test_p4_a_database_error_in_the_dry_run_is_not_a_refusal_of_the_rules(pg_session, clock, monkeypatch, caplog):
    """Hand: the dry run asks whether the lesson is still in the schedule.  The schedule says «нет такого урока» (an ``OpsError``) -> ``restore_allowed`` false, 200.  A broken database (OperationalError)
    is NOT «нельзя вернуть»: it goes out (the call fails), is written to the log with its trace, and no ``restore_allowed: false`` is answered."""
    from sqlalchemy.exc import OperationalError

    from src.auth import lesson_substitutions as LS
    from src.auth.ops_rules import not_found

    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    real = LS.Substitutions._lesson_ref
    monkeypatch.setattr(LS.Substitutions, "_lesson_ref", lambda self, *a, **k: (_ for _ in ()).throw(not_found("LESSON_NOT_FOUND", "Урока нет")))
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert d["restore_allowed"] is False
    monkeypatch.setattr(LS.Substitutions, "_lesson_ref", lambda self, *a, **k: (_ for _ in ()).throw(OperationalError("SELECT 1", {}, Exception("the server closed the connection"))))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(OperationalError):
            client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", params={"dry_run": "true"},
                                   json={"lessonDate": PAST_MON, "lessonId": MON, "cancellationKind": "centre", "expectedKind": "unconfirmed"})
    assert any(r.exc_info for r in caplog.records)


# ============================================================================================ п.5 the one who decided is gone
def test_p5_the_decider_is_not_in_the_database_the_answer_is_409_without_a_name(pg_session, clock):
    """Hand: the owner confirms ``centre`` (the audit line names him); the user of that line is then absent (the state is forged in the test database: the foreign key is switched off for one UPDATE).  A
    second owner sends another kind with ``expectedKind unconfirmed``: 409 ``ALREADY_DECIDED`` (not 500), ``decided_by_name`` null, the words «другой сотрудник»."""
    from uuid import uuid4

    w, g, people, members = world(pg_session, clock, money_right=True)
    teacher_cancels(w, g)
    set_kind(w, g, "centre")
    w.db.execute(text("SET session_replication_role = replica"))
    w.db.execute(text("UPDATE group_attendance_audit SET actor_id = :gone WHERE action = 'corrected' AND details ? 'cancellation_kind'"), {"gone": str(uuid4())})
    w.db.execute(text("SET session_replication_role = DEFAULT"))
    w.db.commit()
    d = set_kind(w, g, "holiday", who="admin", expect=409).json()["detail"]
    assert d["code"] == "ALREADY_DECIDED" and d["kind"] == "centre" and d["decided_by_name"] is None and d["message"].startswith("Отмену подтвердил другой сотрудник")


# ============================================================================================ п.6 ``teacherCanMark`` is the function of the window
def test_p6_teacher_can_mark_is_read_from_the_function_of_the_window(pg_session, clock, monkeypatch):
    """Hand: the lesson of 23.11 (3 days old, window 7) is given back by the teacher: ``teacherCanMark`` true.  When the ONE function of the window (``lesson_window.is_too_old_for_teacher``) says «too
    old», the flag follows it: false.  (A copy of the comparison beside the function would stay true.)"""
    from src.auth import lesson_window as LW

    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    withdraw(w, g)
    assert [i["teacherCanMark"] for i in mine(w) if i["status"] == "cancelled"] == [True]
    monkeypatch.setattr(LW, "is_too_old_for_teacher", lambda *a, **k: True)
    assert [i["teacherCanMark"] for i in mine(w) if i["status"] == "cancelled"] == [False]


# ============================================================================================ п.7 the catalogue says what the road says
def test_p7_the_catalogue_text_of_lesson_too_old_is_the_function_of_the_text_with_the_default_days():
    """Hand: the default window is 7 days: «Урок старше 7 дн. — его закрывает администратор или владелец.» - the text of the road for the default window and the text of the catalogue are ONE."""
    from src.auth import centre_policy as cp
    from src.auth.ops_rules import ERROR_CATALOGUE, lesson_too_old_text

    default = cp.KEYS[cp.LESSON_WINDOW_DAYS].default
    assert default == 7 and ERROR_CATALOGUE["LESSON_TOO_OLD"] == (403, lesson_too_old_text(default)) and lesson_too_old_text(7) == "Урок старше 7 дн. — его закрывает администратор или владелец."


# ============================================================================================ п.8 the border of the day: by the day the lesson was STARTED to be arranged
def test_p8_arranged_at_23_58_in_its_own_day_closed_at_00_02_with_nobody_came_is_allowed(pg_session, clock):
    """Hand: Wed 25.11.  The teacher «оформил» it at 23:58 on 25.11 (the row is created on the lesson's own day) and at 00:02 on 26.11 completes it with nobody present (``confirmNobodyCame``): the
    day it was arranged = the day of the lesson, so not «задним числом»: 200 (the old code compared with the moment of the completion: 409 ``NOBODY_CAME_BACKDATED``).  Control: arranged on 26.11
    00:02 (the day after) and completed at once with nobody: 409 ``NOBODY_CAME_BACKDATED``."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 23:58")
    reconcile(w, g, "2026-11-25")
    clock.set("2026-11-26 00:02")
    save(w, g, people, "2026-11-25", confirmNobodyCame=True)
    w2, g2, p2, m2 = world(pg_session, clock)
    clock.set("2026-11-26 00:02")
    reconcile(w2, g2, "2026-11-25")
    r = save(w2, g2, p2, "2026-11-25", confirmNobodyCame=True, expect=409)
    assert r.json()["detail"]["code"] == "NOBODY_CAME_BACKDATED"


# ============================================================================================ п.9 an evening lesson closed after midnight is not «задним числом»
def _evening(pg_session, w):
    g = new_group(pg_session, w.org, w.ids, "GE", "Вечер", w.A, [new_slot("lesson-eve", 3, "21:00", "22:00", "2026-01-01")])
    s = new_student(pg_session, w.org, w.ids, "ev1", "Вечерний", "Тест", g)
    pg_session.commit()
    return g, s


def _held_evening(w, g, s, closed_at, clock):
    clock.set("2026-11-25 21:05")
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/start", json={"lessonDate": "2026-11-25", "lessonId": "lesson-eve"})
    assert r.status_code == 200, r.text
    clock.set(closed_at)
    save(w, g, [s], "2026-11-25", lesson="lesson-eve", marks={str(s.id): "present"})


def test_p9_a_lesson_started_in_time_and_closed_after_midnight_is_no_entry_two_days_later_it_is(pg_session, clock):
    """Hand: the lesson Wed 25.11 21:00-22:00 is STARTED at 21:05 (``started_at`` filled).  Closed at 00:10 on 26.11 (2 h 10 min after its end, the dates differ by 1 day): NO entry of the history (the old
    rule counted the dates: «1 день»).  Another lesson closed on 27.11 12:00 (two days after the date, ~38 hours after the end): an entry ``backdated``, ``lateDays`` 2, «на 2 дня позже даты урока»."""
    w, g0, people, members = world(pg_session, clock)
    g, s = _evening(pg_session, w)
    _held_evening(w, g, s, "2026-11-26 00:10", clock)
    assert [i for i in listing(w, status="decided")["items"] if i.get("source") == "audit"] == []
    assert [i for i in listing(w, status="decided", backdated_only="true")["items"] if i.get("source") == "audit"] == []
    w2, g20, p20, m20 = world(pg_session, clock)
    g2, s2 = _evening(pg_session, w2)
    _held_evening(w2, g2, s2, "2026-11-27 12:00", clock)
    (e,) = [i for i in listing(w2, status="decided")["items"] if i.get("source") == "audit"]
    assert (e["kind"], e["lateDays"], e["text"]) == ("backdated", 2, "Урок оформлен задним числом: на 2 дня позже даты урока")


# ============================================================================================ п.10 «Отозвать» is the right of the AUTHOR
def test_p10_the_administrator_without_the_right_withdraws_his_own_cancellation_at_any_age_the_others_may_not(pg_session, clock):
    """Hand: the administrator (no money right) cancels the past lesson of 23.11 by the decision of a request (3 days old; the window of the centre set to 1).  His own cancellation: the row of «Ждут
    решения» says ``canWithdraw`` true for HIM (a teacher's window does not bind an administrator), false for the owner (not the author); the road: the owner's «Отозвать» 403 ``NOT_YOUR_CANCELLATION``, the
    teacher A 403; the administrator 200 (the old road answered 403 ``LESSON_TOO_OLD`` for the window of a teacher); the lesson is back in the list «без отметки», the audit names the administrator."""
    w, g, people, members = world(pg_session, clock)
    admin_cancels_past_by_decision(w, g, clock)
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    rows = {who: next(i for i in listing(w, who, status="pending")["items"] if i["source"] == "unconfirmed") for who in ("admin", "owner")}
    assert (rows["admin"]["canWithdraw"], rows["owner"]["canWithdraw"]) == (True, False)
    assert withdraw(w, g, "owner", expect=403).json()["detail"]["code"] == "NOT_YOUR_CANCELLATION"
    assert withdraw(w, g, "A", expect=403).json()["detail"]["code"] in ("NOT_YOUR_CANCELLATION", "LESSON_TOO_OLD")
    withdraw(w, g, "admin")
    assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9
    from src.models.auth import GroupAudit

    w.db.expire_all()
    (audit,) = w.db.query(GroupAudit).filter_by(group_id=g.id, action="lesson_restored").all()
    assert audit.actor_id == w.admin.id and audit.actor_role == "admin"


def test_p10_the_teacher_keeps_his_window_and_the_flag_and_the_road_are_one_rule(pg_session, clock):
    """Hand: the teacher cancels 23.11.  Window 7 (age 3): the flag of «Ждут решения» for the owner is false (not the author), the teacher's own ``canWithdraw`` (block of the teacher) true, the road 200 for
    him.  Window 1 (age 3 > 1): the teacher's flag false and the road 403 ``LESSON_TOO_OLD`` (the window binds a TEACHER)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    assert mine(w)[0]["canWithdraw"] is False and withdraw(w, g, expect=403).json()["detail"]["code"] == "LESSON_TOO_OLD"
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 7}).status_code == 200
    assert mine(w)[0]["canWithdraw"] is True
    row = next(i for i in listing(w, "owner", status="pending")["items"] if i["source"] == "unconfirmed")
    assert row["canWithdraw"] is False
    withdraw(w, g)


# ============================================================================================ п.11 + п.16 the words of the teacher's block follow the KIND
def test_p11_a_past_cancellation_decided_by_an_administrator_without_the_right_waits_in_the_block_of_the_teacher(pg_session, clock):
    """Hand: A asked for Mon 23.11, on 26.11 the administrator without the right decided «Отменить»: the lesson row is ``unconfirmed``.  The teacher's block says ``cancel_waiting`` (the old words:
    ``cancelled``, chosen by the road ``decide``, not by the kind).  After the owner's confirmation ``centre``: ``cancelled_centre``."""
    w, g, people, members = world(pg_session, clock)
    admin_cancels_past_by_decision(w, g, clock)
    assert [i["outcome"] for i in mine(w) if i["lesson"]["date"] == PAST_MON] == ["cancel_waiting"]
    set_kind(w, g, "centre")
    assert [i["outcome"] for i in mine(w) if i["lesson"]["date"] == PAST_MON] == ["cancelled_centre"]


@pytest.mark.parametrize("kind, words", [("centre", "centre"), ("holiday", "holiday")])
def test_p16_the_teacher_reads_the_kind_of_a_confirmed_cancellation_in_the_event_too(pg_session, clock, kind, words):
    """Hand: the teacher cancels, the owner confirms ``centre`` (or ``holiday``): the block says ``cancelled_{centre|holiday}`` AND the event «подтверждена» of his own record, read by the teacher, carries the
    kind ``to`` the same (no sum anywhere: no ``amount`` / ``total`` / ``per_student`` in the event)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    set_kind(w, g, kind)
    (row,) = [i for i in mine(w) if i["lesson"]["date"] == PAST_MON]
    assert row["outcome"] == f"cancelled_{words}"
    detail = client(w, "A").get(f"{SUB}/{row['id']}").json()
    (event,) = [e for e in detail["events"] if e["action"] == "confirmed"]
    assert event["snapshot"]["cancellationKind"]["to"] == kind and "reason" not in event["snapshot"]
    assert all(k not in str(event) for k in ("amount", "total", "per_student"))


# ============================================================================================ п.12 the age of a waiting row is the moment of the cancellation
def test_p12_a_day_off_set_and_voided_does_not_reset_the_waiting_days_nor_the_order(pg_session, clock):
    """Hand: A cancels Mon 23.11 on 24.11 (waits 2 days on 26.11) and Wed 25.11 on 25.11 13:00 (waits 1 day).  On 26.11 the owner sets a day off on 23.11 and voids it: the row of 23.11 is touched
    (``updated_at`` = 26.11) but was cancelled on 24.11.  Expected: ``waitingDays`` 2 and 1; the order of «Ждут решения» (the oldest wait first): 23.11 then 25.11 (the old code: 0 and 1, the order 25.11,
    23.11)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-24 12:00")
    teacher_cancels(w, g)
    clock.set("2026-11-25 13:00")
    teacher_cancels(w, g, day="2026-11-25", lesson=WED)
    clock.set("2026-11-26 12:00")
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert client(w, "owner").post(f"{API}/org/days-off/{r.json()['dayOff']['id']}/void").status_code == 200
    rows = [i for i in listing(w, status="pending")["items"] if i["source"] == "unconfirmed"]
    assert [(i["lesson"]["date"], i["waitingDays"]) for i in rows] == [(PAST_MON, 2), ("2026-11-25", 1)]


# ============================================================================================ п.13 the history loses only the waiting record
def test_p13_a_rejected_request_of_the_lesson_stays_in_the_history_while_the_cancellation_waits(pg_session, clock):
    """Hand: A asked for Mon 23.11 on 22.11, the owner rejected it (a record ``rejected``); on 26.11 A cancelled the lesson «Урока не было» (waits).  The history lists the REJECTED record (the old filter
    dropped every record of the lesson) and not the waiting cancellation; after the confirmation both records are there."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-22 12:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": PAST_MON}], "reasonCode": "sick"})
    rid = r.json()["created"][0]["id"]
    assert client(w, "owner").post(f"{SUB}/{rid}/decide", json={"decision": "reject", "reason": "Нельзя"}).status_code == 200
    clock.set("2026-11-26 12:00")
    teacher_cancels(w, g)
    got = listing(w, status="decided")["items"]
    assert [(i["id"], i["status"]) for i in got if i.get("source") == "request"] == [(rid, "rejected")]
    set_kind(w, g, "centre")
    assert sorted(i["status"] for i in listing(w, status="decided")["items"] if i.get("source") == "request") == ["cancelled", "rejected"]


# ============================================================================================ п.14 why the dry run says «0 students»
def test_p14_zero_reason_packages(pg_session, clock):
    """Hand: the package mode (8 lessons): no recalculation of a cancellation (the lesson is simply not written off): ``total`` 0, ``zero_reason`` ``packages``, the words of the server."""
    w, g, people, members = world(pg_session, clock, period_mode="lessons", lessons_per_payment=8, billing_anchor="payment_date")
    teacher_cancels(w, g)
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert (d["total"], d["zero_reason"], d["zero_text"]) == (0, "packages", "В пакетах уроков отмена не требует перерасчёта: урок просто не списывается.")


def test_p14_zero_reason_nobody_charged(pg_session, clock):
    """Hand: nobody of the group paid for that lesson (the only student joined on 24.11, after Mon 23.11): ``total`` 0, ``zero_reason`` ``nobody_charged``."""
    w, g, people, members = world(pg_session, clock, students=0)
    new_student(pg_session, w.org, w.ids, "late1", "Новичок", "Тест", g, joined=datetime(2026, 11, 24, 6, 0))
    pg_session.commit()
    teacher_cancels(w, g)
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert (d["total"], d["zero_reason"], d["zero_text"]) == (0, "nobody_charged", "За этот урок никто из учеников не платит: перерасчёта нет.")


def test_p14_zero_reason_one_off(pg_session, clock):
    """Hand: a one-off (extra) lesson of 25.11 15:00-16:00 was added on 24.11 and is cancelled by the teacher after its time (waits): the confirmation has nothing to recalculate: ``total`` 0,
    ``zero_reason`` ``one_off``."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-24 12:00")
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-11-25", "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
    assert r.status_code in (200, 201), r.text
    lesson_id = r.json()["id"]
    clock.set("2026-11-26 12:00")
    teacher_cancels(w, g, day="2026-11-25", lesson=lesson_id)
    d = set_kind(w, g, "centre", day="2026-11-25", lesson=lesson_id, dry_run=True).json()
    assert (d["total"], d["zero_reason"], d["zero_text"]) == (0, "one_off", "Разовый урок: перерасчёта нет.")


def test_p14_no_zero_reason_when_there_is_a_sum(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert d["total"] == 450_000 and d["zero_reason"] is None and d["zero_text"] is None


# ============================================================================================ п.15 the migration lr1 has its lock timeout
def test_p15_the_migration_lr1_sets_the_lock_timeout_like_lr2_and_lr3():
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    for prefix in ("lr1", "lr2", "lr3"):
        (path,) = list(versions.glob(f"{prefix}*.py"))
        text_ = path.read_text(encoding="utf-8")
        assert text_.count("SET LOCAL lock_timeout = '5s'") >= 2, (path.name, "upgrade and downgrade must both set the lock timeout")


# ============================================================================================ the tests that were missing
def test_a_change_of_the_billing_mode_between_the_cancellation_and_the_confirmation_does_not_move_the_old_lesson(pg_session, clock):
    """Hand: the teacher cancels Mon 23.11 on 24.11; on 25.11 the owner switches the centre to packages of 8 lessons (acts from 25.11); on 26.11 the owner confirms ``centre``.  The lesson of 23.11 belongs
    to the regime of its DAY (the shared month): 450 000 / 9 = 50 000 for each of the nine - the same as in the world where the owner cancelled it at once on 26.11 (the oracle)."""
    seen = []
    for how in ("A", "B"):
        w, g, people, members = world(pg_session, clock)
        if how == "A":
            clock.set("2026-11-24 12:00")
            teacher_cancels(w, g)
        clock.set("2026-11-25 12:00")
        r = client(w, "owner").put(f"{API}/org/settings/payments", json={"period_mode": "lessons", "lessons_per_payment": 8})
        assert r.status_code == 200, r.text
        clock.set("2026-11-26 12:00")
        if how == "A":
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        seen.append(accounts(w, members))
    assert seen[0] == seen[1] == NINE_X_50, seen


def test_the_confirmation_in_the_next_month_recalculates_the_month_of_the_lesson(pg_session, clock):
    """Hand: the teacher cancels Mon 23.11 on 24.11; the owner confirms on Tue 01.12 10:00.  The lesson is NOVEMBER's: 450 000 / 9 = 50 000 each, the line «Перерасчёт» is dated by the LESSON (23.11),
    not by the confirmation; December is charged 450 000 and is covered by the 50 000 credit: unpaid 450 000 - 50 000 = 400 000, advance 0.  The oracle: the owner cancelling ``centre`` on 01.12 at once."""
    seen = []
    for how in ("A", "B"):
        w, g, people, members = world(pg_session, clock)
        if how == "A":
            clock.set("2026-11-24 12:00")
            teacher_cancels(w, g)
        clock.set("2026-12-01 10:00")
        if how == "A":
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        st = statement(w, members[0])
        seen.append((accounts(w, members), [(r["date"], r["type"], r["credit"]) for r in st["rows"] if r["type"] == "recalc"]))
    assert seen[0] == seen[1]
    assert seen[0][0] == [(0, 400_000, 50_000)] * 9 and seen[0][1] == [("2026-11-23", "recalc", 50_000)], seen[0]


def _makeup_world(pg_session, clock):
    """16.11 cancelled by the owner (17.11); a make-up for it on Thu 26.11 10:00-11:30 appointed on 18.11; now 26.11 12:00 (the make-up has begun, no row: it is «ahead» and LIFTS the deduction)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-17 12:00")
    cancel(w, g, MON, "2026-11-16", kind="centre", who="owner")
    clock.set("2026-11-18 12:00")
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-11-26", "startTime": "10:00", "endTime": "11:30", "purpose": "makeup",
                                                                   "madeUpLessonId": MON, "madeUpDate": "2026-11-16"})
    assert r.status_code in (200, 201), r.text
    clock.set("2026-11-26 12:00")
    assert accounts(w, members) == [(0, 0, 0)] * 9
    return w, g, members, r.json()["id"]


def test_a_waiting_cancellation_of_a_makeup_lesson_does_not_move_the_account(pg_session, clock):
    """Hand (``ledger_service._load_makeups``): Mon 16.11 cancelled «по вине центра» (50 000 each); the make-up for it is «ahead» (today, not closed) and lifts the deduction: (0, 0, 0).  The teacher A
    cancels the make-up «Урока не было»: a PAST lesson, so the cancellation WAITS, and a waiting cancellation is neutral for the money: the accounts must stay (0, 0, 0)."""
    w, g, members, makeup = _makeup_world(pg_session, clock)
    teacher_cancels(w, g, day="2026-11-26", lesson=makeup)
    assert kind_in_db(w, g, "2026-11-26", makeup) == ("cancelled", "unconfirmed")
    assert accounts(w, members) == [(0, 0, 0)] * 9


def test_the_confirmation_of_a_cancelled_makeup_follows_the_makeup_rule(pg_session, clock):
    """Hand: after the owner confirms ``centre`` the make-up is cancelled by the centre (``gone``): the lesson of 16.11 is made up by nobody, its deduction is back: 50 000 each - the same as in the
    world where the owner cancels the make-up at once (the oracle)."""
    seen = []
    for how in ("A", "B"):
        w, g, members, makeup = _makeup_world(pg_session, clock)
        if how == "A":
            teacher_cancels(w, g, day="2026-11-26", lesson=makeup)
            set_kind(w, g, "centre", day="2026-11-26", lesson=makeup)
        else:
            cancel(w, g, makeup, "2026-11-26", kind="centre", who="owner")
        seen.append(accounts(w, members))
    assert seen[0] == seen[1] == NINE_X_50, seen


# ============================================================================================ «отзыв против подтверждения»: two REAL threads, both orders
@pytest.mark.parametrize("first", ["withdraw", "confirm"])
def test_withdraw_against_confirmation_two_threads_both_orders(pg_session, clock, first):
    """Hand: the cancellation of 23.11 waits.  The teacher's withdrawal and the owner's confirmation start at once (real threads, own sessions, the group row held until both wait; the first in the
    queue wins).  ``withdraw`` first: the withdrawal 200 (the lesson is back, accounts 0), the confirmation 409 ``ALREADY_DECIDED`` (outcome ``withdrawn_by_teacher``).  ``confirm`` first: the confirmation
    200 (nine x 50 000), the withdrawal 409 ``ALREADY_DECIDED`` (kind ``centre``).  Never both, never none."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    w.db.commit()
    engine = w.db.get_bind()
    calls = {
        "withdraw": lambda: own_session_client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": PAST_MON, "lessonId": MON, "expectedKind": "unconfirmed"}),
        "confirm": lambda: own_session_client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind",
                                                              json={"lessonDate": PAST_MON, "lessonId": MON, "cancellationKind": "centre", "expectedKind": "unconfirmed"}),
    }
    order = [first, "confirm" if first == "withdraw" else "withdraw"]
    answers = {}
    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT id FROM groups WHERE id = :g FOR UPDATE"), {"g": str(g.id)})
    threads = []
    try:
        for i, name in enumerate(order):
            t = threading.Thread(target=lambda n=name: answers.__setitem__(n, calls[n]()))
            t.start()
            threads.append(t)
            assert _waiting(engine, i + 1), f"{name} did not reach the lock of the group"
    finally:
        trans.commit()
        holder.close()
        for t in threads:
            t.join(timeout=30)
    if first == "withdraw":
        assert answers["withdraw"].status_code == 200 and answers["confirm"].status_code == 409, {k: v.text[:200] for k, v in answers.items()}
        assert answers["confirm"].json()["detail"]["outcome"] == "withdrawn_by_teacher"
        assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9
    else:
        assert answers["confirm"].status_code == 200 and answers["withdraw"].status_code == 409, {k: v.text[:200] for k, v in answers.items()}
        assert answers["withdraw"].json()["detail"]["kind"] == "centre"
        assert kind_in_db(w, g) == ("cancelled", "centre") and accounts(w, members) == NINE_X_50
