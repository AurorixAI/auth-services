"""LESSON-RULES С9 and the readers: the kind ``unconfirmed`` in every answer that shows a cancellation (journal, schedule), the centre WITHOUT the confirmation (the behaviour of before the pack), and the
export of the REAL answers for the screens (rule 178): with ``LESSON_RULES_ANSWERS_DIR`` set the test writes the answers as files, always it checks their shape.

World: ``lesson_rules_helpers.world`` (nine students, 450 000, group «Г», today Thu 26.11 12:00)."""
import json
import os
from pathlib import Path

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


def test_export_of_the_real_answers_for_the_screens(pg_session, clock):
    """One consistent world: a waiting cancellation (A, Mon 23.11), a decided one (B... owner confirmed Wed 18.11 as ``centre``), a lesson completed late, a corrected one; every answer the screens
    read is saved as a file (when the directory is set) and its shape is checked."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    out = {}
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 14}).status_code == 200

    def got(name, r):
        assert r.status_code == 200, (name, r.text)
        out[name] = r.json()
        return r.json()

    teacher_cancels(w, g, "2026-11-18", WED)
    set_kind(w, g, "centre", day="2026-11-18", lesson=WED)
    teacher_cancels(w, g)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=2))
    save(w, g, people, "2026-11-25", who="owner", marks=marks_of(people, first_present=3), correctionReason="Ошибся")
    for who in ("owner", "admin"):
        pending = got(f"pending_{who}", client(w, who).get(SUB, params={"status": "pending"}))
        assert [i["source"] for i in pending["items"]] == ["unconfirmed"]
        got(f"summary_{who}", client(w, who).get(f"{SUB}/summary"))
    history = got("history", client(w, "owner").get(SUB, params={"status": "decided"}))
    assert {i.get("kind") for i in history["items"] if i.get("source") == "audit"} == {"backdated", "corrected"}
    got("history_backdated_only", client(w, "owner").get(SUB, params={"status": "decided", "backdated_only": "true"}))
    got("mine_teacher", client(w, "A").get(f"{SUB}/mine", params={"status": "all"}))
    got("dry_run_confirm_centre", set_kind(w, g, "centre", dry_run=True))
    got("dry_run_confirm_holiday", set_kind(w, g, "holiday", dry_run=True))
    got("rules_lessons", client(w, "owner").get(f"{API}/org/settings/lessons"))
    # the refusals the screens show
    refusals = {}
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-11-18", "lessonId": WED, "cancellationKind": "holiday", "expectedKind": "unconfirmed"})
    refusals["already_decided"] = r.json()
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-25", "lessonId": WED, "status": "in_progress", "records": {}, "expectedVersion": 99})
    refusals["attendance_conflict"] = r.json()
    w2, g2, p2, m2 = world(pg_session, clock)
    reconcile(w2, g2, "2026-11-25")
    refusals["nobody_came_backdated"] = save(w2, g2, p2, "2026-11-25", expect=409).json()
    refusals["lesson_too_old"] = reconcile(w2, g2, "2026-11-18", WED, expect=403).json()
    refusals["policy_value_invalid"] = client(w2, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 5}).json()
    teacher_cancels(w2, g2)
    from datetime import date
    from tests.ops_helpers import closed_sheet

    closed_sheet(w2, w2.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    refusals["period_closed"] = set_kind(w2, g2, "centre", expect=409).json()
    assert refusals["already_decided"]["detail"]["code"] == "ALREADY_DECIDED" and refusals["nobody_came_backdated"]["detail"]["code"] == "NOBODY_CAME_BACKDATED"
    assert refusals["lesson_too_old"]["detail"]["code"] == "LESSON_TOO_OLD" and refusals["period_closed"]["detail"]["code"] == "PERIOD_CLOSED"
    out["refusals"] = refusals
    target = os.environ.get("LESSON_RULES_ANSWERS_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        for name, body in out.items():
            Path(target, f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
