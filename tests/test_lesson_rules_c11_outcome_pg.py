"""LESSON-RULES С11 п.19: the outcome of a lesson that was cancelled and then GIVEN BACK is not «отменён» - in the journal of the group, in the schedule and in the block of the teacher.  One place chooses
«the last event of the lesson» (``lesson_cancel.lesson_outcomes``).

World: ``lesson_rules_helpers.world`` (today Thu 26.11 12:00 - the clock is FROZEN, so the cancellation and the return carry the same instant: the choice must not depend on the order of the ids).
Lesson Mon 23.11 10:00-11:30 of the teacher A.  The expectation of each test is in its comment BEFORE the call."""
import pytest

from tests.lesson_rules_helpers import MON, PAST_MON, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def journal_lesson(w, g, who="A"):
    r = client(w, who).get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    return next(l for l in r.json()["lessons"] if l.get("date") == PAST_MON and l.get("id") == MON)


def schedule_lesson(w, who="A"):
    r = client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-23", "date_to": "2026-11-23"})
    assert r.status_code == 200, r.text
    items = r.json() if isinstance(r.json(), list) else r.json().get("items") or r.json().get("occurrences")
    return next(i for i in items if i.get("occurrenceDate") == PAST_MON and i.get("id") == MON)


def mine_cancelled(w, who="A"):
    r = client(w, who).get(f"{SUB}/mine", params={"status": "all"})
    return [i["outcome"] for i in r.json()["items"] if i["lesson"]["date"] == PAST_MON and i["status"] == "cancelled"]


def outcomes(w, g):
    return (journal_lesson(w, g).get("substitutionOutcome"), schedule_lesson(w).get("substitutionOutcome"))


@pytest.mark.parametrize("attempt", range(4))          # the frozen clock makes ties; the ids are random: several worlds so that an order by the ids cannot pass by luck
def test_cancelled_then_given_back_then_held_has_no_cancelled_outcome(pg_session, clock, attempt):
    """Hand: the teacher cancels (waits), the owner gives the lesson back («Урок был»), the owner arranges and completes it with nine present.  The last event of the lesson is the RETURN (and then the
    holding): the journal and the schedule carry NO ``substitutionOutcome`` (it was «cancelled» before: the chip «Урок отменён» next to «Завершён»); the teacher's block says «урок возвращён» /
    «включён в зарплату», never «отменён» / «ждёт»."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    reconcile(w, g, PAST_MON, MON, who="owner")
    save(w, g, people, PAST_MON, MON, who="owner", marks=marks_of(people, first_present=9))
    assert outcomes(w, g) == (None, None)
    assert mine_cancelled(w) == ["included_in_payroll"]


@pytest.mark.parametrize("attempt", range(4))          # the frozen clock makes ties; the ids are random: several worlds so that an order by the ids cannot pass by luck
def test_cancelled_by_a_role_without_the_right_then_withdrawn_is_neither_cancelled_nor_waiting(pg_session, clock, attempt):
    """Hand: the teacher cancels (waits) and withdraws his own cancellation: the lesson is back to «без отметки».  Journal and schedule: no outcome; the block: «lesson_restored» (not ``cancel_waiting``)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": PAST_MON, "lessonId": MON}).status_code == 200
    assert outcomes(w, g) == (None, None)
    assert mine_cancelled(w) == ["lesson_restored"]


@pytest.mark.parametrize("attempt", range(4))          # the frozen clock makes ties; the ids are random: several worlds so that an order by the ids cannot pass by luck
def test_cancelled_given_back_and_cancelled_again_is_cancelled_by_its_kind(pg_session, clock, attempt):
    """Hand: cancelled (waits) -> given back -> cancelled again by the teacher (waits): the journal and the schedule say «cancelled» (the lesson is cancelled again), the block ``cancel_waiting``; the owner
    confirms ``centre``: the block ``cancelled_centre``, the journal still «cancelled»."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": PAST_MON, "lessonId": MON}).status_code == 200
    teacher_cancels(w, g)
    assert [o["kind"] if o else None for o in outcomes(w, g)] == ["cancelled", "cancelled"]
    assert sorted(mine_cancelled(w)) == ["cancel_waiting", "lesson_restored"]          # the first record of the lesson (given back) and the new one (waits)
    set_kind(w, g, "centre")
    assert [o["kind"] if o else None for o in outcomes(w, g)] == ["cancelled", "cancelled"] and sorted(mine_cancelled(w)) == ["cancelled_centre", "lesson_restored"]


# ============================================================================================ п.17 «История»: one line for a lesson that was given back
def test_the_history_has_one_line_for_a_given_back_lesson_the_record_of_the_return(pg_session, clock):
    """Hand: the teacher cancels Mon 23.11, the owner gives the lesson back.  The history of the owner has ONE line for that lesson: the record of the return (``closed`` / ``lesson_restored``: who gave it back,
    when); the cancellation record with the outcome ``lesson_restored`` is not listed (two lines «Урок возвращён» before).  The block of the teacher is unchanged: his cancellation record, outcome
    ``lesson_restored``.  A lesson cancelled again after the return: its waiting cancellation is in «Ждут решения», the history keeps the one return line."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    rows = [i for i in client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"] if i.get("source") == "request" and i["lesson"]["date"] == PAST_MON]
    assert [(i["status"], i["outcome"]) for i in rows] == [("closed", "lesson_restored")], rows
    assert mine_cancelled(w) == ["lesson_restored"]
    teacher_cancels(w, g)
    rows = [i for i in client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"] if i.get("source") == "request" and i["lesson"]["date"] == PAST_MON]
    assert [(i["status"], i["outcome"]) for i in rows] == [("closed", "lesson_restored")], rows
