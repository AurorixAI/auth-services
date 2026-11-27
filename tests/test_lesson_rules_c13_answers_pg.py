"""LESSON-RULES C13: the small corrections of the answers the screens need (PostgreSQL).  Every expectation is in the comment BEFORE the call.

World: ``lesson_rules_helpers.world``: group «Г» of the teacher A, Mon / Wed 10:00-11:30, nine students at 450 000 for November (9 lessons by the divisor: 50 000 a lesson), now Thu 26.11 12:00; the past
lesson is Mon 23.11 (3 days old, the window of the centre 7 days)."""
from __future__ import annotations

import pytest

from src.auth import money_effects as me
from tests.lesson_rules_helpers import MON, PAST_MON, reconcile, save, set_kind, teacher_cancels, world, client, API, clock, pg_session, needs_pg  # noqa: F401
from tests.payf_helpers import cancel
from tests.test_lesson_rules_c10_server_pg import journal, schedule
from tests.test_lesson_rules_c11_rules_pg import RULES, SUB, listing

pytestmark = needs_pg
NBSP = " "


# ============================================================================================ 1. the preview after somebody else's decision is ALWAYS the refusal
def test_c13_1_the_preview_after_the_decision_of_another_is_409_the_real_call_of_the_same_kind_is_still_200(pg_session, clock):
    """Hand: A cancels 23.11 (waits).  The owner confirms ``centre``.  The second owner's window still shows ``unconfirmed``: its PREVIEW (dry_run) of ``centre`` with ``expectedKind = unconfirmed`` is 409
    ``ALREADY_DECIDED`` (who / when / the kind), not 200 ``changed: false`` without the fields of the preview; the REAL call of the same kind stays 200 ``changed: false`` (rule 7.5)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    set_kind(w, g, "centre")
    r = set_kind(w, g, "centre", dry_run=True, expect=409)
    assert r.json()["detail"]["code"] == "ALREADY_DECIDED" and r.json()["detail"]["kind"] == "centre", r.text
    r = set_kind(w, g, "centre")
    assert r.json()["changed"] is False


# ============================================================================================ 2. ``waiting`` in every branch of the answer of a cancellation
def test_c13_2_waiting_in_every_branch(pg_session, clock):
    """Hand (23.11 is past and begun, the centre asks the owner to confirm): a cancellation by the teacher A, by the administrator WITHOUT the right (the dialog «Урока не было», the journal, the decision of a
    request in «Замены») WAITS: ``waiting: true`` and the sentence of the waiting (the administrator's: ``WAITING_CONSEQUENCE``; the teacher's: ``WAITING_CONSEQUENCE_TEACHER``).  The owner's cancellation does not
    wait: ``waiting: false`` in the dialog, no ``waiting`` in the answer of the journal (it says it only when true)."""
    for who, how, words in (("admin", "reconcile", me.WAITING_CONSEQUENCE), ("A", "reconcile", me.WAITING_CONSEQUENCE_TEACHER), ("admin", "journal", me.WAITING_CONSEQUENCE)):
        w, g, people, members = world(pg_session, clock)
        r = teacher_cancels(w, g, who=who) if how == "reconcile" else cancel(w, g, MON, PAST_MON, who=who)
        assert r.json()["waiting"] is True and r.json()["consequence"] == words, (who, how, r.text)
    w, g, people, members = world(pg_session, clock)
    assert teacher_cancels(w, g, who="owner").json()["waiting"] is False
    w, g, people, members = world(pg_session, clock)
    assert "waiting" not in cancel(w, g, MON, PAST_MON, who="owner").json()
    w, g, people, members = world(pg_session, clock)          # the decision of a request of A by the administrator without the right
    clock.set("2026-11-22 12:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": PAST_MON}], "reasonCode": "sick"})
    assert r.status_code == 201, r.text
    rid = r.json()["created"][0]["id"]
    clock.set("2026-11-26 12:00")
    d = client(w, "admin").post(f"{SUB}/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"})
    assert d.status_code == 200 and d.json()["result"]["waiting"] is True and d.json()["result"]["consequence"] == me.WAITING_CONSEQUENCE, d.text


# ============================================================================================ 3. the preview of the cancellation of a past lesson says ``waiting``
def test_c13_3_cancel_consequence_says_waiting_by_the_right_and_the_setting(pg_session, clock):
    """Hand: the lesson of 23.11 is past and begun.  The administrator without the right and the teacher A: the cancellation WOULD wait (``waiting: true``, the waiting sentence); the owner: no (he decides).  The
    centre turns ``past_cancel_needs_confirmation`` off: nobody waits any more (``waiting: false``) - the ordinary cancellation, its sentence is the ordinary one (not the waiting one)."""
    w, g, people, members = world(pg_session, clock)

    def ask(who):
        r = client(w, who).get(f"{API}/org/groups/{g.id}/lessons/cancel-consequence", params={"lessonDate": PAST_MON, "lessonId": MON})
        assert r.status_code == 200, r.text
        return r.json()

    assert [ask(x)["waiting"] for x in ("admin", "A", "owner")] == [True, True, False]
    assert ask("admin")["consequence"] == me.WAITING_CONSEQUENCE and ask("A")["consequence"] == me.WAITING_CONSEQUENCE_TEACHER
    assert client(w, "owner").put(RULES, json={"past_cancel_needs_confirmation": False}).status_code == 200
    assert [ask(x)["waiting"] for x in ("admin", "A", "owner")] == [False, False, False]
    assert ask("admin")["consequence"] != me.WAITING_CONSEQUENCE


# ============================================================================================ 4 + 6. the journal and the schedule: who reported, may I withdraw
def test_c13_4_6_the_journal_and_the_schedule_say_who_reported_and_whether_the_viewer_may_withdraw(pg_session, clock):
    """Hand: A cancels 23.11 (waits).  Journal and schedule, for A: ``waiting`` true, ``canWithdraw`` true (his own, age 3 inside the window 7), ``reportedBy`` the short name of A, ``reportedByRole``
    ``teacher``; for the owner and the administrator: the same words, ``canWithdraw`` false (not the author); every other lesson: ``waiting`` false, no name, ``canWithdraw`` null.  Then the window is 1
    day: A may not withdraw it any more (age 3 > 1: false).  Another world: the administrator without the right cancels: ``reportedByRole`` ``admin``, ``canWithdraw`` true for HIM (a window is the teacher's),
    false for A."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    name = lambda x: x["reportedBy"]
    for read in (lambda who: journal(w, g, who), lambda who: schedule(w, who)):
        a, owner, admin = read("A")[PAST_MON], read("owner")[PAST_MON], read("admin")[PAST_MON]
        assert (a["waiting"], a["canWithdraw"], a["reportedByRole"]) == (True, True, "teacher") and name(a) == name(owner) == name(admin) and name(a)
        assert (owner["canWithdraw"], admin["canWithdraw"], owner["reportedByRole"]) == (False, False, "teacher")
        other = read("A")["2026-11-16"]
        assert (other["waiting"], other["reportedBy"], other["reportedByRole"], other["canWithdraw"]) == (False, None, None, None)
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    assert journal(w, g, "A")[PAST_MON]["canWithdraw"] is False and schedule(w, "A")[PAST_MON]["canWithdraw"] is False
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g, who="admin")
    for read in (lambda who: journal(w, g, who), lambda who: schedule(w, who)):
        admin, a = read("admin")[PAST_MON], read("A")[PAST_MON]
        assert (admin["reportedByRole"], admin["canWithdraw"], a["canWithdraw"], a["reportedByRole"]) == ("admin", True, False, "admin")


def test_c13_6_the_waiting_rows_name_who_reported(pg_session, clock):
    """Hand: A cancels 23.11: the row of «Ждут решения» for the owner has ``reportedBy`` (the short name of A) and ``reportedByRole`` ``teacher``; the administrator cancels another world's lesson: ``admin``."""
    for who, role in (("A", "teacher"), ("admin", "admin")):
        w, g, people, members = world(pg_session, clock)
        teacher_cancels(w, g, who=who)
        row = next(i for i in listing(w, "owner", status="pending")["items"] if i["source"] == "unconfirmed")
        assert row["reportedByRole"] == role and row["reportedBy"] and row["reportedBy"] == row["cancelledBy"]["name"], row


# ============================================================================================ 5. the words of every outcome come from the server
def test_c13_5_the_preview_carries_the_words_of_every_outcome(pg_session, clock):
    """Hand: A cancels 23.11 (waits).  Preview of the confirmation (any asked kind): ``centre_text`` = «Перерасчёт 9 ученикам по 50 000 сум, всего 450 000 сум. Преподавателю урок не оплачивается.»
    (450 000 / 9 lessons = 50 000 each, nine students); ``holiday_text`` = «Ученикам: без перерасчёта. Преподавателю урок не оплачивается.»; ``held_text`` (age 3 inside the window 7) = «Отмена снимается,
    преподаватель отметит посещаемость. Деньги считаются как за обычный урок.».  Window 1: «Отмена снимается. Урок старше 1 дн.: отметит администратор или владелец.».  The recalculation of the centre
    never on: ``centre_text`` = «Перерасчёт за отмены в центре выключен — ученикам ничего не изменится. Преподавателю урок не оплачивается.»  The preview writes nothing (the cancellation still waits)."""
    from tests.lesson_rules_helpers import kind_in_db

    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    centre = f"Перерасчёт 9 ученикам по 50{NBSP}000{NBSP}сум, всего 450{NBSP}000{NBSP}сум. Преподавателю урок не оплачивается."
    holiday = "Ученикам: без перерасчёта. Преподавателю урок не оплачивается."
    held = "Отмена снимается, преподаватель отметит посещаемость. Деньги считаются как за обычный урок."
    for asked in ("centre", "holiday"):
        p = set_kind(w, g, asked, dry_run=True).json()
        assert (p["centre_text"], p["holiday_text"], p["held_text"]) == (centre, holiday, held), (asked, p)
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    assert set_kind(w, g, "centre", dry_run=True).json()["held_text"] == "Отмена снимается. Урок старше 1 дн.: отметит администратор или владелец."
    w, g, people, members = world(pg_session, clock, recalc=False)          # the recalculation was never switched on (a switch acts from its day, never back on 23.11: SPEC 7.2)
    teacher_cancels(w, g)
    assert set_kind(w, g, "centre", dry_run=True).json()["centre_text"] == "Перерасчёт за отмены в центре выключен — ученикам ничего не изменится. Преподавателю урок не оплачивается."


# ============================================================================================ 7. ``canReturn`` of an entry of the history
def test_c13_7_the_entry_of_the_history_says_whether_the_lesson_may_be_returned(pg_session, clock):
    """Hand: Wed 25.11 is arranged on 26.11 00:02 and completed at 00:10 (one entry ``backdated``).  The owner (the right of money): ``canReturn`` true (the lesson is held, the group active, the month
    open); the administrator without the right: false.  After the owner returned the lesson to the mark (the road ``attendance/return``): the entry says false for the owner too (it is not held any more)."""
    from tests.ops_helpers import seen_version

    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-26 00:02")
    reconcile(w, g, "2026-11-25")
    clock.set("2026-11-26 00:10")
    save(w, g, people, "2026-11-25", marks={str(s.id): "present" for s in people})

    def entry(who):
        (e,) = [i for i in listing(w, who, status="decided")["items"] if i.get("source") == "audit"]
        return e["canReturn"]

    assert (entry("owner"), entry("admin")) == (True, False)
    wed = next(i for i in schedule(w, "owner").values() if i["occurrenceDate"] == "2026-11-25")["id"]
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance/return", json={"lessonDate": "2026-11-25", "lessonId": wed, "reason": "Ошибка в отметках", **seen_version(w, g, "2026-11-25", wed)})
    assert r.status_code == 200, r.text
    assert entry("owner") is False
