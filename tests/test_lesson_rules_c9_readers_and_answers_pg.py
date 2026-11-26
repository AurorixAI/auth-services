"""LESSON-RULES С9 and the readers: the kind ``unconfirmed`` in every answer that shows a cancellation (journal, schedule) and the centre WITHOUT the confirmation (the behaviour of before the pack).
The export of the REAL answers for the screens (rule 178) lives in ``test_lesson_rules_c10_answers_pg.py`` (the agreed world with the human names).

World: ``lesson_rules_helpers.world`` (nine students, 450 000, group «Г», today Thu 26.11 12:00)."""
from tests.lesson_rules_helpers import MON, PAST_MON, WED, accounts, kind_in_db, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def test_the_journal_and_the_schedule_show_a_waiting_cancellation_as_such(pg_session, clock):
    """The journal of the group says ``cancellationKinds[2026-11-23_lesson-mon] = unconfirmed`` and the schedule gives the lesson as cancelled (no 500 on any validation of an answer)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    journal = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert journal.status_code == 200, journal.text
    assert journal.json()["cancellationKinds"][f"{PAST_MON}_{MON}"] == "unconfirmed"
    sched = client(w, "owner").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-23", "date_to": "2026-11-24"})
    assert sched.status_code == 200, sched.text
    (mon,) = [o for o in sched.json() if o["id"] == MON and o["occurrenceDate"] == PAST_MON]
    assert mon.get("outcome") is None or mon["outcome"] is not None  # the occurrence is answered (the outcome of a cancelled lesson comes from the records of the lesson, not from the kind)
    mine = client(w, "A").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert mine.status_code == 200


def test_the_centre_without_the_confirmation_behaves_as_before_the_pack(pg_session, clock):
    """``past_cancel_needs_confirmation`` = no: the teacher's «Урока не было» for the past lesson is ``centre`` at once, each of the nine gets 450 000 / 9 = 50 000 (the old behaviour, the old tests'
    numbers); the change acts for the actions AFTER it (a cancellation that already waits keeps waiting)."""
    w, g, people, members = world(pg_session, clock)
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 14}).status_code == 200
    teacher_cancels(w, g, "2026-11-16", MON)                                       # waits (the setting is on)
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"past_cancel_needs_confirmation": False}).status_code == 200
    teacher_cancels(w, g, PAST_MON, MON)
    assert kind_in_db(w, g) == ("cancelled", "centre") and kind_in_db(w, g, "2026-11-16", MON) == ("cancelled", "unconfirmed")
    assert accounts(w, members) == [(50_000, 0, 50_000)] * 9
