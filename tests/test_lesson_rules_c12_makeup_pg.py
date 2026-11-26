"""LESSON-RULES C12.1: the WAITING cancellation of a make-up lesson moves no account.

The world (hand, ``_makeup_world``): nine students, 450 000 each for November, the regular lessons Mon + Wed (the month has 9 lessons by the divisor: 450 000 / 9 = 50 000 a lesson).
Mon 16.11 is cancelled «по вине центра» (17.11); a make-up for it on Thu 26.11 10:00-11:30 is appointed (18.11); now 26.11 12:00 (the make-up has begun, no journal row: «ahead» and it LIFTS the
deduction of 16.11): every account (advance, unpaid, recalculated) = (0, 0, 0).

The oracle (read in ``one_off_purpose.release`` / ``lesson_cancel._cancel_locked`` / ``ledger_service._load_makeups`` BEFORE the fix): the OWNER cancels the make-up (kind ``centre`` or ``holiday``) -
the pointer ``made_up_by`` of 16.11 is released at once, the make-up is ``gone``, the deduction of 16.11 is back: 50 000 each -> accounts (50 000, 0, 50 000) x 9  (= ``NINE_X_50``: the month is paid, the
line «Перерасчёт» gives 50 000 back: advance 50 000, unpaid 0, recalculated 50 000).

Hand, branch by branch (the rule of the pack: a waiting cancellation is neutral for the money; the pointer is released when the kind is CONFIRMED, as the owner's cancellation does it at once):
  1. the make-up cancelled by the teacher («Урока не было», it is past: ``unconfirmed``): pointer stays, the make-up is still counted as «ahead» -> (0, 0, 0) x 9.
  2. + the owner confirms ``centre`` on 26.11:  the same as the owner cancelling ``centre`` at once on 26.11 -> NINE_X_50 (both worlds, equal to the last number).
  3. + the owner confirms ``holiday``: the oracle is the owner cancelling the make-up as ``holiday`` at once: release, 50 000 each -> NINE_X_50 (the cancelled LESSON of 16.11 is still «по вине центра»).
  4. + the author withdraws («Отозвать»): the make-up is back on the mark list, the pointer was never moved -> (0, 0, 0) x 9 before, during and after; the pointer of 16.11 is the make-up.
  5. + the owner «Урок был»: the same return to the mark -> (0, 0, 0) x 9 at every step.
  6. a SECOND make-up for 16.11 while the cancellation of the first waits: refused 409 ``MAKEUP_EXISTS`` (the pointer still holds the first: one lesson is never made up twice); nothing changes.
"""
from __future__ import annotations

import pytest

from src.models.auth import GroupAttendance
from tests.lesson_rules_helpers import MON, accounts, kind_in_db, set_kind, teacher_cancels, client, API, clock, pg_session  # noqa: F401
from tests.payf_helpers import cancel
from tests.test_lesson_rules_c11_rules_pg import NINE_X_50, _makeup_world, withdraw

DAY = "2026-11-26"
ZERO = [(0, 0, 0)] * 9


def pointer(w, g):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-11-16", lesson_id=MON).one().made_up_by


def test_1_waiting_cancel_keeps_the_pointer_and_the_account(pg_session, clock):
    w, g, members, makeup = _makeup_world(pg_session, clock)
    teacher_cancels(w, g, day=DAY, lesson=makeup)
    assert kind_in_db(w, g, DAY, makeup) == ("cancelled", "unconfirmed")
    assert accounts(w, members) == ZERO
    assert pointer(w, g) == makeup


@pytest.mark.parametrize("kind", ["centre", "holiday"])
def test_2_3_confirmation_is_the_owner_cancelling_at_once(pg_session, clock, kind):
    seen = []
    for how in ("A", "B"):
        w, g, members, makeup = _makeup_world(pg_session, clock)
        if how == "A":
            teacher_cancels(w, g, day=DAY, lesson=makeup)
            assert accounts(w, members) == ZERO and pointer(w, g) == makeup
            set_kind(w, g, kind, day=DAY, lesson=makeup)
        else:
            cancel(w, g, makeup, DAY, kind=kind, who="owner")
        seen.append((accounts(w, members), pointer(w, g)))
    assert seen[0] == seen[1] == (NINE_X_50, None), seen


def test_4_withdraw_leaves_the_account_and_the_pointer(pg_session, clock):
    w, g, members, makeup = _makeup_world(pg_session, clock)
    teacher_cancels(w, g, day=DAY, lesson=makeup)
    assert accounts(w, members) == ZERO
    withdraw(w, g, day=DAY, lesson=makeup)
    assert kind_in_db(w, g, DAY, makeup) is None
    assert accounts(w, members) == ZERO and pointer(w, g) == makeup


def test_5_the_lesson_was_leaves_the_account_and_the_pointer(pg_session, clock):
    w, g, members, makeup = _makeup_world(pg_session, clock)
    teacher_cancels(w, g, day=DAY, lesson=makeup)
    assert accounts(w, members) == ZERO
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": DAY, "lessonId": makeup, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200, r.text
    assert kind_in_db(w, g, DAY, makeup) is None
    assert accounts(w, members) == ZERO and pointer(w, g) == makeup


def test_6_a_second_makeup_while_the_first_cancellation_waits_is_refused(pg_session, clock):
    w, g, members, makeup = _makeup_world(pg_session, clock)
    teacher_cancels(w, g, day=DAY, lesson=makeup)
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-11-27", "startTime": "10:00", "endTime": "11:30", "purpose": "makeup",
                                                                   "madeUpLessonId": MON, "madeUpDate": "2026-11-16"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MAKEUP_EXISTS", r.text
    assert accounts(w, members) == ZERO and pointer(w, g) == makeup


# ============================================================================================ the same in the three billing modes
MODES = {
    "shared": {},                                                                                   # «общий месяц»: calendar, fixed day 1 (the default of ``world``)
    "personal": {"billing_anchor": "payment_date"},                                                 # «персональный месяц»: the period of a student starts on his payment date
    "packages": {"period_mode": "lessons", "lessons_per_payment": 8},                               # «пакеты»: 8 lessons a package
}


# Hand: shared / personal month - 450 000 paid, nobody's lessons are counted as held, the deduction of 16.11 is 450 000 / 9 = 50 000: (0, 0, 0) while the make-up stands, (50 000, 0, 50 000) once it is
# gone (advance 50 000 and the line «Перерасчёт» 50 000).  Packages of 8: the package of 450 000 is spent by HELD lessons only; a make-up that was never held and a cancellation of it (waiting or
# confirmed) spend nothing, the cancelled lesson of 16.11 is no spent lesson either and no money line exists in this mode: (450 000, 0, 0) in every state - the package does not move.
EXPECT = {"shared": ((0, 0, 0), (50_000, 0, 50_000)), "personal": ((0, 0, 0), (50_000, 0, 50_000)), "packages": ((450_000, 0, 0), (450_000, 0, 0))}


def _mode_world(pg_session, clock, block):
    from tests.lesson_rules_helpers import world

    w, g, people, members = world(pg_session, clock, **block)
    clock.set("2026-11-17 12:00")
    cancel(w, g, MON, "2026-11-16", kind="centre", who="owner")
    clock.set("2026-11-18 12:00")
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": DAY, "startTime": "10:00", "endTime": "11:30", "purpose": "makeup",
                                                                   "madeUpLessonId": MON, "madeUpDate": "2026-11-16"})
    assert r.status_code in (200, 201), r.text
    clock.set("2026-11-26 12:00")
    return w, g, members, r.json()["id"]


@pytest.mark.parametrize("mode", list(MODES))
def test_7_the_waiting_cancellation_of_a_makeup_in_each_mode(pg_session, clock, mode):
    """Hand, every mode: the account BEFORE the cancellation of the make-up is the reference ``R`` (the make-up lifts the deduction of 16.11).  Waiting cancel / withdraw: ``R`` again (neutral).
    Confirmation ``centre`` = the owner cancelling at once (the oracle), in the same mode."""
    ends = []
    for how in ("A", "B"):
        w, g, members, makeup = _mode_world(pg_session, clock, MODES[mode])
        ref = accounts(w, members)
        if how == "A":
            teacher_cancels(w, g, day=DAY, lesson=makeup)
            assert accounts(w, members) == ref and pointer(w, g) == makeup
            set_kind(w, g, "centre", day=DAY, lesson=makeup)
        else:
            cancel(w, g, makeup, DAY, kind="centre", who="owner")
        ends.append((accounts(w, members), pointer(w, g)))
    assert ends[0] == ends[1], ends
    assert (ref[0], ends[0][0][0]) == EXPECT[mode], (mode, ref[0], ends[0][0][0])
    w, g, members, makeup = _mode_world(pg_session, clock, MODES[mode])
    ref = accounts(w, members)
    teacher_cancels(w, g, day=DAY, lesson=makeup)
    withdraw(w, g, day=DAY, lesson=makeup)
    assert accounts(w, members) == ref and pointer(w, g) == makeup


# ============================================================================================ C12.2 «задним числом» by the day the arrangement was BEGUN
def _audit(w):
    from tests.test_lesson_rules_c11_rules_pg import listing

    return [(i["kind"], i["lateDays"]) for i in listing(w, status="decided")["items"] if i.get("source") == "audit"]


def test_8_arranged_in_its_own_day_completed_after_midnight_is_no_entry_arranged_the_day_after_is(pg_session, clock):
    """Hand (Tashkent clock, Wed 25.11): arranged («Оформить») at 23:58 on 25.11, completed with the marks at 00:02 on 26.11: the day the arrangement began = the day of the lesson -> 0 days
    late -> NO entry «задним числом» (the old rule counted the completion: 1 day -> an entry); arranged 26.11 00:02 and completed 00:10: 26.11 - 25.11 = 1 day -> ONE entry ``backdated``, 1 day;
    arranged in its own day and completed two days later (27.11): still 0 days (the base is the beginning) -> no entry."""
    from tests.lesson_rules_helpers import reconcile, save, world

    cases = [("2026-11-25 23:58", "2026-11-26 00:02", []), ("2026-11-26 00:02", "2026-11-26 00:10", [("backdated", 1)]), ("2026-11-25 23:58", "2026-11-27 12:00", [])]
    for began, done, expected in cases:
        w, g, people, members = world(pg_session, clock)
        clock.set(began)
        reconcile(w, g, "2026-11-25")
        clock.set(done)
        save(w, g, people, "2026-11-25", marks={str(s.id): "present" for s in people})
        assert _audit(w) == expected, (began, done, _audit(w))
