"""LESSON-RULES С2: the CONFIRMATION of a waiting cancellation (``PUT .../attendance/cancellation-kind``, ``expectedKind``, ``dry_run``) and the equality of worlds.

World: ``lesson_rules_helpers.world`` (group «Г» Mon / Wed, 9 lessons in November, 450 000, shared month, recalculation on from 01.11, nine students who paid 450 000; today Thu 26.11).  By hand:
one lesson «по вине центра» deducts 450 000 / 9 = 50 000 from each of the nine (450 000 in all).  The teacher A cancels Mon 23.11 (3 days ago) -> ``unconfirmed``.  The expectation of each test is in
its comment BEFORE the call; where a world differs only by WHEN the same cancellation was decided, the oracle is the other world («the teacher + the confirmation on D» = «the owner on D»)."""
from datetime import date

import pytest

from src.models.auth import GroupAttendance, GroupAttendanceAudit, LessonSubstitutionEvent, LessonSubstitutionRequest

from tests.lesson_rules_helpers import MON, PAST_MON, WED, account, accounts, kind_in_db, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from tests.money1_helpers import changed_tables, db_fingerprint
from tests.ops_helpers import closed_sheet
from tests.payf_helpers import D, cancel, pay, pause

pytestmark = needs_pg
NINE_X_50 = [(50_000, 0, 50_000)] * 9


# ================================================================================================ the four outcomes of the world «450 000 / 9 lessons»
def test_world_1_the_teacher_cancels_nothing_moves_then_each_outcome(pg_session, clock):
    """(1) the teacher cancels 23.11: the nine accounts (0, 0, 0) and the statement do not move;  (2) the owner confirms ``centre`` on 26.11: each of the nine gets 50 000 (450 000 in all)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert accounts(w, members) == [(0, 0, 0)] * 9
    r = set_kind(w, g, "centre")
    assert r.json() == {"ok": True, "changed": True, "cancellationKind": "centre", "version": r.json()["version"]}
    assert accounts(w, members) == NINE_X_50 and sum(a[2] for a in accounts(w, members)) == 450_000


def test_world_1_holiday_changes_nothing(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    set_kind(w, g, "holiday")
    assert kind_in_db(w, g) == ("cancelled", "holiday") and accounts(w, members) == [(0, 0, 0)] * 9


def test_world_1_the_lesson_was_returns_to_the_list_without_a_mark_and_changes_nothing(pg_session, clock):
    """«Урок был» = the existing return to the mark (``lessons/restore``): the row is gone, the lesson is in the list «без отметки» again (for the teacher: his, 3 days old, in the window), accounts (0, 0, 0)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200 and r.json()["previousKind"] == "unconfirmed" and not r.json().get("deductions"), r.text
    assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9
    open_ = client(w, "A").get(f"{API}/org/attendance/open-actions").json()
    assert (PAST_MON, MON) in {(i["lessonDate"], i["lessonId"]) for i in open_}


# ================================================================================================ the dry run
def test_the_dry_run_says_what_the_confirmation_does_and_writes_nothing(pg_session, clock):
    """Hand: nine students, 50 000 each, 450 000 in all; the recalculation is on at the day of the lesson; the group is active and the lesson is 3 days old (the window is 7: its teacher could still
    mark it).  The dry run writes NOTHING (every table of the database is the same before and after) and its numbers are the numbers of the real answer."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    before = db_fingerprint(w.db)
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert changed_tables(before, db_fingerprint(w.db)) == []
    assert (d["dry_run"], d["from"], d["to"], d["students"], d["per_student"], d["total"], d["recalc_off"]) == (True, "unconfirmed", "centre", 9, 50_000, 450_000, False)
    assert (d["restore_allowed"], d["age_days"], d["window_days"], d["teacher_can_mark"]) == (True, 3, 7, True)
    assert sorted(x["amount"] for x in d["deductions"]) == [50_000] * 9
    h = set_kind(w, g, "holiday", dry_run=True).json()
    assert (h["students"], h["per_student"], h["total"]) == (0, None, 0)
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
    set_kind(w, g, "centre")
    assert sum(a[2] for a in accounts(w, members)) == d["total"]


def test_the_dry_run_of_an_unequal_group_says_no_single_figure(pg_session, clock):
    """One of the nine is paused on the day of the lesson (23.11): he has nothing deducted (hand: he was not charged for it) -> 8 students x 50 000 = 400 000; the figures are equal among the eight."""
    w, g, people, members = world(pg_session, clock)
    pause(w, members[8], D(2026, 11, 23), D(2026, 11, 23))
    teacher_cancels(w, g)
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert (d["students"], d["total"]) == (8, 400_000) and d["per_student"] == 50_000


def test_the_dry_run_names_a_switched_off_recalculation_by_the_day_of_the_lesson(pg_session, clock):
    """SPEC 7.2.  (a) on at the lesson (23.11), switched OFF on 25.11, confirmation 26.11: the ledger judges by the day of the lesson -> still 50 000 each, ``recalc_off`` false.
    (b) a centre with the recalculation OFF at the lesson, switched ON on 25.11: the lesson is older than the switch -> 0, ``recalc_off`` true."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    clock.set("2026-11-25 12:00")
    assert client(w, "owner").put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": False}).status_code == 200
    clock.set("2026-11-26 12:00")
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert (d["recalc_off"], d["students"], d["total"]) == (False, 9, 450_000)
    set_kind(w, g, "centre")
    assert accounts(w, members) == NINE_X_50
    w2, g2, people2, members2 = world(pg_session, clock, recalc=False)
    teacher_cancels(w2, g2)
    clock.set("2026-11-25 12:00")
    assert client(w2, "owner").put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    clock.set("2026-11-26 12:00")
    d2 = set_kind(w2, g2, "centre", dry_run=True).json()
    assert (d2["recalc_off"], d2["students"], d2["total"]) == (True, 0, 0)
    set_kind(w2, g2, "centre")
    assert accounts(w2, members2) == [(0, 0, 0)] * 9


# ================================================================================================ the rules of the road
def test_the_confirmation_needs_no_reason_a_change_of_a_decided_kind_does(pg_session, clock):
    """unconfirmed -> centre without a reason: 200.  centre -> holiday without a reason: 422 ``REASON_REQUIRED``; with one: 200.  NULL is refused (422), ``unconfirmed`` is refused (422)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert set_kind(w, g, None, expect=422).status_code == 422
    assert set_kind(w, g, "unconfirmed", expect=422).status_code == 422
    set_kind(w, g, "centre")
    r = set_kind(w, g, "holiday", expected=None, expect=422)
    assert r.json()["detail"]["code"] == "REASON_REQUIRED"
    set_kind(w, g, "holiday", expected=None, reason="Это был праздник")
    assert kind_in_db(w, g) == ("cancelled", "holiday") and accounts(w, members) == [(0, 0, 0)] * 9


def test_the_audit_and_the_journal_of_the_requests_carry_the_confirmation_without_a_second_record(pg_session, clock):
    """The audit says ``{from: unconfirmed, to: centre}``; the record of the cancellation in the journal «Замены» gets ONE event ``confirmed`` (who, when) and NO new record is made."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    n_before = w.db.query(LessonSubstitutionRequest).filter_by(org_id=w.org.id).count()
    set_kind(w, g, "centre")
    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=PAST_MON, lesson_id=MON).one()
    kinds = [a.details["cancellation_kind"] for a in w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).order_by(GroupAttendanceAudit.created_at) if isinstance(a.details.get("cancellation_kind"), dict)]
    assert kinds == [{"from": "unconfirmed", "to": "centre"}]
    assert w.db.query(LessonSubstitutionRequest).filter_by(org_id=w.org.id).count() == n_before
    events = [e for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id) if e.action == "confirmed"]
    assert len(events) == 1 and events[0].actor_id == w.owner.id and events[0].snapshot["cancellationKind"] == {"from": "unconfirmed", "to": "centre"}


def test_the_rights_the_teacher_and_the_administrator_without_the_right_cannot_decide(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    for who in ("A", "admin"):
        r = set_kind(w, g, "centre", who=who, expect=403)
        assert r.status_code == 403
        assert set_kind(w, g, "centre", who=who, dry_run=True, expect=403).status_code == 403
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed") and accounts(w, members) == [(0, 0, 0)] * 9


def test_a_closed_month_refuses_the_confirmation(pg_session, clock):
    """The sheet of the teacher for November is saved (the month is closed): 409 ``PERIOD_CLOSED``, the row stays ``unconfirmed``, accounts 0."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    closed_sheet(w, w.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    r = set_kind(w, g, "centre", expect=409)
    assert r.json()["detail"]["code"] == "PERIOD_CLOSED"
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed") and accounts(w, members) == [(0, 0, 0)] * 9


# ================================================================================================ two people, a double press, a withdrawal
def test_a_double_press_by_the_same_owner_is_one_change_and_the_second_answer_is_200_unchanged(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    first = set_kind(w, g, "centre").json()
    second = set_kind(w, g, "centre").json()
    assert first["changed"] is True and second == {"ok": True, "changed": False, "cancellationKind": "centre", "version": first["version"]}
    assert accounts(w, members) == NINE_X_50


def test_two_owners_the_second_with_another_kind_gets_already_decided_with_who_and_when(pg_session, clock):
    """The first owner confirms ``centre`` at 12:00; the second, who saw ``unconfirmed``, sends ``holiday``: 409 ``ALREADY_DECIDED`` with ``kind: centre``, the name, the role, the time; nothing changes."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    set_kind(w, g, "centre", who="owner")
    r = set_kind(w, g, "holiday", who="owner", expect=409)
    d = r.json()["detail"]
    assert d["code"] == "ALREADY_DECIDED" and d["kind"] == "centre" and d["decided_by_role"] == "owner" and d["decided_by_name"]
    assert d["message"].startswith("Отмену подтвердил ") and "(владелец) в 12:00: по вине центра." in d["message"] and d["decided_at"]
    assert kind_in_db(w, g) == ("cancelled", "centre") and accounts(w, members) == NINE_X_50
    r2 = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "ALREADY_DECIDED" and r2.json()["detail"]["kind"] == "centre"        # «Урок был» does not silently delete a confirmed one
    assert accounts(w, members) == NINE_X_50


def test_the_teacher_withdraws_before_the_owner_decides_the_owner_gets_already_decided(pg_session, clock):
    """C3 is the teacher's own withdrawal; here its trace for the owner: the cancellation is GONE (restored by whoever), the owner's confirmation with ``expectedKind`` -> 409 ``ALREADY_DECIDED``,
    ``kind`` null, outcome ``lesson_returned`` (an owner returned it) - and without ``expectedKind`` the old 404 ``LESSON_NOT_CANCELLED`` stays."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    d = set_kind(w, g, "centre", expect=409).json()["detail"]
    assert d["code"] == "ALREADY_DECIDED" and d["kind"] is None and d["outcome"] == "lesson_returned"
    assert set_kind(w, g, "centre", expected=None, reason="xxx", expect=404).json()["detail"]["code"] == "LESSON_NOT_CANCELLED"


# ================================================================================================ the equality of worlds (LESSONS 156)
def _twin(pg_session, clock, between, expected, **opts):
    """World A: the teacher cancels 23.11 on 24.11, ``between`` happens on 25.11, the owner confirms ``centre`` on 26.11.  World B: ``between`` on 25.11, the owner cancels ``centre`` on 26.11.
    Returns ``(A accounts, B accounts)``; both must be ``expected`` (by hand) and equal."""
    out = []
    for salt, mode in (("a", "A"), ("b", "B")):
        w, g, people, members = world(pg_session, clock, **opts)
        if mode == "A":
            clock.set("2026-11-24 12:00")
            teacher_cancels(w, g)
        clock.set("2026-11-25 12:00")
        between(w, g, people, members)
        clock.set("2026-11-26 12:00")
        if mode == "A":
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        out.append(accounts(w, members))
    assert out[0] == out[1], (out[0], out[1])
    assert out[0] == expected, out[0]
    return out


def test_worlds_equal_a_plain_confirmation(pg_session, clock):
    """Hand: nine x (50 000 advance, 0 unpaid, 50 000 recalculated) in both worlds."""
    _twin(pg_session, clock, lambda *a: None, NINE_X_50)


def test_worlds_equal_a_payment_between_the_cancellation_and_the_confirmation(pg_session, clock):
    """The first student pays 100 000 more on 25.11.  Hand: his advance = 100 000 (December ahead) + 50 000 (the deduction on 26.11) = 150 000; the other eight 50 000.  Both worlds: the payment came
    BEFORE the day the centre's cancellation became known (26.11), so the order of the events is the same."""
    _twin(pg_session, clock, lambda w, g, p, m: pay(w, m[0], 100_000, D(2026, 11, 25)), [(150_000, 0, 50_000)] + [(50_000, 0, 50_000)] * 8)


def test_worlds_equal_a_refund_between(pg_session, clock):
    """The first student paid 100 000 more on 24.11 and gets 100 000 back on 25.11 (a refund of the advance): the advance is 0 again; then the deduction 50 000 on 26.11 -> advance 50 000 like the others."""
    def between(w, g, people, members):
        pay(w, members[0], 100_000, D(2026, 11, 24))
        r = client(w, "owner").post(f"{API}/org/memberships/{members[0].id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "Вернули"})
        assert r.status_code == 201, r.text

    _twin(pg_session, clock, between, NINE_X_50)


def test_worlds_equal_a_pause_that_covers_the_day_of_the_lesson(pg_session, clock):
    """The last student is put on a pause 23.11-23.11 on 25.11 (after the cancellation).  Hand: the recalculation of the cancelled lesson is 0 for him (the lesson was inside his pause: he was not charged
    for it), 50 000 for the other eight; his account is identical in both worlds (the oracle), the eight are 50 000 / 0 / 50 000."""
    out = _twin_free(pg_session, clock, lambda w, g, p, m: pause(w, m[8], D(2026, 11, 23), D(2026, 11, 23)))
    assert [a[2] for a in out[0]] == [50_000] * 8 + [0]


def _twin_free(pg_session, clock, between):
    """Like ``_twin`` but the absolute expectation is given by the caller (an account that a pause / a stay moves by its own rules is compared with the other world, the hand figure is asserted by the test)."""
    out = []
    for salt, mode in (("a", "A"), ("b", "B")):
        w, g, people, members = world(pg_session, clock)
        if mode == "A":
            clock.set("2026-11-24 12:00")
            teacher_cancels(w, g)
        clock.set("2026-11-25 12:00")
        between(w, g, people, members)
        clock.set("2026-11-26 12:00")
        if mode == "A":
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        out.append(accounts(w, members))
    assert out[0] == out[1], (out[0], out[1])
    return out


def test_worlds_equal_a_student_who_left_between(pg_session, clock):
    """The ninth student leaves the group on 25.11 (after the lesson of 23.11).  Oracle: the other world; the eight who stayed are 50 000 each (hand)."""
    from tests.pg_world import leave_group

    out = _twin_free(pg_session, clock, lambda w, g, p, m: leave_group(w.db, w.org.id, g.id, p[8].id, w.owner.id))
    assert out[0][:8] == [(50_000, 0, 50_000)] * 8


def test_worlds_equal_the_schedule_template_changed_between(pg_session, clock):
    """The owner stops the Monday series from 30.11 on 25.11 (the lesson of 30.11 is gone): November has 4 Mondays (2, 9, 16, 23) + 4 Wednesdays (4, 11, 18, 25) = 8 lessons, so a lesson is worth
    450 000 / 8 = 56 250 -> the rounding of the product to whole thousands = 56 000 (``payment_status.round_1000``, src/auth/payment_status.py:306: 0.25 of a thousand goes down; the ledger takes it at
    billing_ledger.py:1184).  The lead's decision 26.11: 56 000 is accepted (my first hand figure 56 250 forgot the rounding).  The equality of the worlds is the part that must hold: the same figure for
    every student in BOTH worlds (``_twin`` asserts A == B before it asserts the figure)."""
    def between(w, g, people, members):
        r = client(w, "owner").post(f"{API}/org/schedule/lessons/{MON}/recurrence/end", json={"effectiveFrom": "2026-11-30", "reason": "Курс закончен", "groupId": str(g.id)})
        assert r.status_code == 200, r.text

    _twin(pg_session, clock, between, [(56_000, 0, 56_000)] * 9)


def test_worlds_equal_the_recalculation_switched_off_and_on_between(pg_session, clock):
    """On at the lesson (23.11), OFF on 25.11 - the day of the lesson decides: the confirmation on 26.11 gives 50 000 each in both worlds (world B cancels on 26.11 with the setting OFF since 25.11:
    a cancellation made after the switch-off for a lesson dated BEFORE it - the same version of the setting, the day of the lesson)."""
    _twin(pg_session, clock, lambda w, g, p, m: client(w, "owner").put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": False}), NINE_X_50)


@pytest.mark.parametrize("mode", [dict(period_mode="lessons", lessons_per_payment=8, billing_anchor="payment_date")], ids=["package-of-8"])
def test_worlds_equal_in_the_package_mode_the_cancellation_is_neutral(pg_session, clock, mode):
    """The package mode: a cancelled lesson is not counted for the package whoever cancelled it and when - ``unconfirmed`` is neutral like ``centre``: the two worlds are equal at every step and the
    confirmation changes no account (hand: the recalculation of the centre is for the monthly modes, the package keeps its lessons)."""
    seen = []
    for salt, how in (("a", "A"), ("b", "B")):
        w, g, people, members = world(pg_session, clock, **mode)
        base = accounts(w, members)
        if how == "A":
            teacher_cancels(w, g)
            assert accounts(w, members) == base
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        seen.append(accounts(w, members))
    assert seen[0] == seen[1]
