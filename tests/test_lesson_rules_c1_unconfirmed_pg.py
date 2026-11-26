"""LESSON-RULES С1: the kind ``unconfirmed`` - a PAST lesson cancelled by a role without the money right waits for the owner and moves NO account.

World (``lesson_rules_helpers.world``): group «Г» Mon / Wed 10:00-11:30, 9 lessons in November 2026, price 450 000, shared month, recalculation on, nine students who paid 450 000 each (account 0).
Today Thu 26.11 12:00.  By hand: one lesson «по вине центра» deducts 450 000 / 9 = 50 000 from each student (9 x 50 000 = 450 000).
The expectation of each test is in its comment BEFORE the call."""
import pytest

from tests.lesson_rules_helpers import MON, PAST_MON, WED, account, accounts, kind_in_db, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from src.models.auth import GroupAttendance
from tests.ops_helpers import seen_version
from tests.payf_helpers import cancel

pytestmark = needs_pg


def test_the_teacher_cancels_a_past_lesson_and_no_account_moves(pg_session, clock):
    """Teacher A: «Урока не было» for Mon 23.11 (3 days ago).  The row is cancelled / ``unconfirmed``; the nine accounts stay (0 advance, 0 unpaid, 0 recalculated)."""
    w, g, people, members = world(pg_session, clock)
    before = accounts(w, members)
    assert before == [(0, 0, 0)] * 9
    teacher_cancels(w, g)
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
    assert accounts(w, members) == before


def test_the_owner_cancels_the_same_lesson_and_each_account_gets_50_000(pg_session, clock):
    """The control world: the owner cancels «по вине центра» at once - 450 000 / 9 = 50 000 for each of the nine (so ``unconfirmed`` really is the difference)."""
    w, g, people, members = world(pg_session, clock)
    cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
    assert kind_in_db(w, g) == ("cancelled", "centre")
    assert accounts(w, members) == [(50_000, 0, 50_000)] * 9


def test_a_future_lesson_is_cancelled_at_once_by_the_centre_kind(pg_session, clock):
    """The request of a teacher «не смогу провести» for a FUTURE lesson and the cancellation of a future lesson by an administrator without the right: unchanged (Г-16, ``centre``)."""
    w, g, people, members = world(pg_session, clock)
    cancel(w, g, MON, "2026-11-30", who="admin")                                       # Monday 30.11, 4 days ahead, the administrator has no money right
    assert kind_in_db(w, g, "2026-11-30") == ("cancelled", "centre")
    assert accounts(w, members) == [(50_000, 0, 50_000)] * 9


def test_the_administrator_without_the_right_gets_unconfirmed_on_every_road_of_a_past_cancellation(pg_session, clock):
    """P2 / P14 (SPEC 6.2): the same rule on every road, whatever the age of the lesson: (a) «Урока не было» from the list, (b) the lesson sheet (save as cancelled) of a lesson never started,
    (c) the sheet of a lesson that was started by «Оформить» (a row, no marks), (d) the decision «Отменить урок» of a request.  Each ``unconfirmed``; accounts stay 0."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g, "2026-11-16", MON, who="admin")                                # (a) 10 days old: the administrator has no window
    cancel(w, g, WED, "2026-11-18", who="admin")                                         # (b) save as cancelled, never started
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/reconcile", json={"lessonDate": "2026-11-25", "lessonId": WED}).status_code == 200      # the teacher «оформил» (a row, no marks) ...
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-25", "lessonId": WED, "status": "cancelled", "records": {}, "cancellationReason": "Не было", **seen_version(w, g, "2026-11-25", WED)})
    assert r.status_code == 200, r.text                                                  # (c) ... and the administrator without the right cancels the STARTED row from the sheet
    assert [kind_in_db(w, g, d, lid) for d, lid in (("2026-11-16", MON), ("2026-11-18", WED), ("2026-11-25", WED))] == [("cancelled", "unconfirmed")] * 3
    assert accounts(w, members) == [(0, 0, 0)] * 9


def test_the_teacher_who_started_the_lesson_and_cancels_it_empty_gets_unconfirmed_too(pg_session, clock):
    """The road of the teacher's own empty started lesson (``_empty_started_lesson``): the lesson is past, the author has no right -> ``unconfirmed`` as everywhere (one rule, no exception)."""
    w, g, people, members = world(pg_session, clock)
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/reconcile", json={"lessonDate": "2026-11-25", "lessonId": WED}).status_code == 200
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-25", "lessonId": WED, "status": "cancelled", "records": {}, "cancellationReason": "Никого"})
    assert r.status_code in (200, 403, 409), r.text                                       # a started lesson with a full roster: the older refusals decide (the roster is not empty)
    if r.status_code == 200:
        assert kind_in_db(w, g, "2026-11-25", WED) == ("cancelled", "unconfirmed")


def test_the_money_right_chooses_the_kind_as_before(pg_session, clock):
    """An administrator WITH the right (and the owner) choose: ``holiday`` is neutral (accounts 0), ``centre`` is 50 000 each; ``unconfirmed`` sent by a person is a 422 on every road."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    cancel(w, g, MON, PAST_MON, kind="holiday", who="admin")
    assert kind_in_db(w, g) == ("cancelled", "holiday") and accounts(w, members) == [(0, 0, 0)] * 9
    cancel(w, g, WED, "2026-11-25", kind="centre", who="admin")
    assert kind_in_db(w, g, "2026-11-25", WED) == ("cancelled", "centre") and accounts(w, members) == [(50_000, 0, 50_000)] * 9
    for url, body in ((f"{API}/org/groups/{g.id}/attendance", {"lessonDate": "2026-11-18", "lessonId": WED, "status": "cancelled", "records": {}, "cancellationReason": "xxx", "cancellationKind": "unconfirmed"}),
                      (f"{API}/org/groups/{g.id}/attendance/reconcile/cancel", {"lessonDate": "2026-11-18", "lessonId": WED, "cancellationReason": "xxx", "cancellationKind": "unconfirmed"})):
        r = client(w, "owner").post(url, json=body)
        assert r.status_code == 422, (url, r.text)
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": PAST_MON, "lessonId": MON, "cancellationKind": "unconfirmed", "reason": "xxx"})
    assert r.status_code == 422, r.text
    assert kind_in_db(w, g, "2026-11-18", WED) is None                                    # nothing was written by the refused ones


# ================================================================================================ the decision of a request (single and bulk)
def _asks(w, g, day, clock):
    """A is the teacher of ``g``; he asks «не смогу провести» for ``day`` on the 22nd (the lesson is in the future then)."""
    clock.set("2026-11-22 12:00")
    r = client(w, "A").post(f"{API}/org/substitutions", json={"lessons": [{"groupId": str(g.id), "lessonId": MON if day == PAST_MON else WED, "lessonDate": day}], "reasonCode": "sick"})
    assert r.status_code == 201, r.text
    return r.json()["created"][0]["id"]


def test_the_decision_cancel_of_a_request_whose_lesson_has_begun_is_unconfirmed_for_an_administrator_without_the_right(pg_session, clock):
    """P2: A asked on 22.11 for Mon 23.11; on 26.11 (the lesson is past) the administrator WITHOUT the right decides «Отменить урок» -> the row is ``unconfirmed``, the answer says so
    (``cancellationKind`` is the STORED type, no 500), the nine accounts stay 0.  The owner deciding the same -> ``centre`` and 50 000 each."""
    w, g, people, members = world(pg_session, clock)
    rid = _asks(w, g, PAST_MON, clock)
    clock.set("2026-11-26 12:00")
    r = client(w, "admin").post(f"{API}/org/substitutions/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["cancellationKind"] == "unconfirmed"
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed") and accounts(w, members) == [(0, 0, 0)] * 9


def test_the_bulk_decision_cancel_is_unconfirmed_too(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    a = _asks(w, g, PAST_MON, clock)
    b = _asks(w, g, "2026-11-25", clock)
    clock.set("2026-11-26 12:00")
    r = client(w, "admin").post(f"{API}/org/substitutions/decide-bulk", json={"ids": [a, b], "decision": "cancel", "reason": "Заболел"})
    assert r.status_code == 200 and r.json()["updated"] == 2 and r.json()["skipped"] == [], r.text
    assert [x["cancellationKind"] for x in r.json()["results"]] == ["unconfirmed", "unconfirmed"]
    assert [kind_in_db(w, g, d, lid) for d, lid in ((PAST_MON, MON), ("2026-11-25", WED))] == [("cancelled", "unconfirmed")] * 2 and accounts(w, members) == [(0, 0, 0)] * 9


def test_the_owner_deciding_the_same_request_gets_centre_and_50_000_each(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    rid = _asks(w, g, PAST_MON, clock)
    clock.set("2026-11-26 12:00")
    r = client(w, "owner").post(f"{API}/org/substitutions/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"})
    assert r.status_code == 200 and r.json()["result"]["cancellationKind"] == "centre", r.text
    assert accounts(w, members) == [(50_000, 0, 50_000)] * 9


# ================================================================================================ the readers of the kind
def test_a_make_up_of_an_unconfirmed_cancellation_is_refused_with_its_own_reason_code(pg_session, clock):
    """422 ``MAKEUP_NOT_ALLOWED`` / ``reasonCode UNCONFIRMED`` - «Сначала подтвердите отмену урока…» (SPEC 6.3: one contract of the refusals, no new status)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    body = {"groupId": str(g.id), "lessonDate": "2026-11-28", "startTime": "12:00", "endTime": "13:00", "purpose": "makeup", "madeUpLessonId": MON, "madeUpDate": PAST_MON}
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json=body)
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["code"] == "MAKEUP_NOT_ALLOWED" and d["reasonCode"] == "UNCONFIRMED" and d["message"] == "Сначала подтвердите отмену урока: отработку можно назначить после подтверждения"
    listed = client(w, "owner").get(f"{API}/org/groups/{g.id}/makeup-candidates").json()
    assert all(i["lessonDate"] != PAST_MON for i in listed["items"])                 # the lesson is not offered at all until it is confirmed


def test_a_day_off_on_the_date_turns_an_unconfirmed_cancellation_into_a_holiday_and_voiding_it_gives_it_back(pg_session, clock):
    """P10: the owner gives a day off on Mon 23.11: the waiting cancellation becomes a ``holiday`` (nothing to confirm, accounts 0), the audit and the journal speak of «ждёт подтверждения», not of
    «по вине центра»; «Отменить выходной» gives ``unconfirmed`` back (the nine accounts stay 0)."""
    from src.models.auth import GroupAttendanceAudit

    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert kind_in_db(w, g) == ("cancelled", "holiday") and accounts(w, members) == [(0, 0, 0)] * 9
    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=PAST_MON, lesson_id=MON).one()
    assert row.converted_from_kind == "unconfirmed"
    audit = [a.details for a in w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).order_by(GroupAttendanceAudit.created_at) if isinstance(a.details, dict) and isinstance(a.details.get("cancellation_kind"), dict)]
    assert audit[-1]["cancellation_kind"] == {"from": "unconfirmed", "to": "holiday"}
    day_off = r.json()["dayOff"]["id"]
    v = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void")
    assert v.status_code == 200, v.text
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed") and accounts(w, members) == [(0, 0, 0)] * 9


def test_restoring_an_unconfirmed_lesson_answers_with_the_stored_kind_and_changes_no_account(pg_session, clock):
    """«Вернуть на отметку» (owner, past lesson): the answer carries ``previousKind: unconfirmed`` (the stored type: no 500 on the validation of the answer); the row is gone, accounts 0."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"})
    assert r.status_code == 200, r.text
    assert r.json()["previousKind"] == "unconfirmed" and r.json().get("returnedToReview") is True
    assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9


def test_stopping_a_series_from_a_past_date_is_refused_and_the_waiting_cancellation_stays(pg_session, clock):
    """The stop of a series is only from a FUTURE date (``effective_from <= today`` is refused), so the row of Mon 23.11 cannot be swept away by it; and the sweep itself skips ``unconfirmed``."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/schedule/lessons/{MON}/recurrence/end", json={"effectiveFrom": PAST_MON, "reason": "Курс закончен", "groupId": str(g.id)})
    assert r.status_code == 409 and "будущей даты" in r.text, r.text
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
