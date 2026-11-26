"""LESSON-RULES: the tests that the check of the spec (sections 6 and 7) found missing after the steps С0-С9 - the transfer between the cancellation and the confirmation, the property of the
neutral kind over random histories in the three billing modes (the matrix property on the loader of the database), the anchors of the bypass roads (П2, П14), and the windows 1 and 30 x the access
of a substitute and of a former teacher.  The expectation of each test is in its comment BEFORE the call."""
import random

import pytest

from tests.b2_helpers import API as B2API, build_d2, client as b2client, decide, req  # noqa: F401
from tests.lesson_rules_helpers import MON, PAST_MON, WED, accounts, kind_in_db, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.payf_helpers import D, cancel, pay, pause
from tests.test_lesson_rules_c0_policy_pg import ORG, put, reconcile
from tests.test_ops_b2_decide_pg import one, sub_body

pytestmark = needs_pg


# ========================================================================================== the transfer between the cancellation and the confirmation
def test_worlds_equal_a_student_transferred_to_another_group_between(pg_session, clock):
    """Hand: nine students, 50 000 each for the lesson of 23.11 (450 000 / 9).  The ninth is TRANSFERRED to the group «Б» on 25.11 (after the cancellation of 24.11, before the confirmation of 26.11).
    The eight who stayed: 50 000 advance, 0 unpaid, 50 000 recalculated in BOTH worlds (the confirmation by the owner on 26.11 = the cancellation by the owner on 26.11).  The account of the one who left
    is the one of the other world (the oracle: the same function of the facts)."""
    from src.auth.service import AuthService
    from tests.ops_helpers import new_group, new_slot
    from tests.pg_world import give_group_price

    out = []
    for mode in ("A", "B"):
        w, g, people, members = world(pg_session, clock)
        second = new_group(pg_session, w.org, w.ids, f"GB{mode}", f"Б-{mode}", w.A, [new_slot(f"lesson-b-{mode}", 2, "10:00", "11:00", "2026-01-01")])
        give_group_price(pg_session, second, 300_000, valid_from=D(2026, 1, 1))
        pg_session.commit()
        if mode == "A":
            clock.set("2026-11-24 12:00")
            teacher_cancels(w, g)
        clock.set("2026-11-25 12:00")
        AuthService(pg_session).add_student_to_group(str(w.org.id), str(second.id), str(people[8].id), mode="transfer", actor_id=str(w.owner.id), money_right=True, from_group_id=str(g.id))
        pg_session.commit()
        clock.set("2026-11-26 12:00")
        if mode == "A":
            set_kind(w, g, "centre")
        else:
            cancel(w, g, MON, PAST_MON, kind="centre", who="owner")
        out.append(accounts(w, members))
    assert out[0] == out[1], (out[0], out[1])
    assert out[0][:8] == [(50_000, 0, 50_000)] * 8, out[0]


# ========================================================================================== the matrix property on the loader: ``unconfirmed`` moves no account
MODES = {
    "common-month": dict(),
    "personal-period": dict(period_mode="calendar", billing_anchor="payment_date"),
    "package-of-8": dict(period_mode="lessons", lessons_per_payment=8, billing_anchor="payment_date"),
}


def _history(seed, w, g, people, members, step):
    """One random fact of the history (the same call in both worlds): an extra payment, a pause that covers no lesson day of the cancelled one, a payment and its refund, a leaving."""
    from tests.pg_world import leave_group

    rng = random.Random(seed * 100 + step)
    kind = rng.choice(["pay", "pause", "pay-refund", "leave"])
    who = (seed + step) % (len(members) - 1)                 # a different student at each step (a student who left gets no later fact); the last student is never touched: his account is the control of the others
    if kind == "pay":
        pay(w, members[who], rng.choice([50_000, 100_000]), D(2026, 11, 24 + step))
    elif kind == "pause":
        pause(w, members[who], D(2026, 11, 25), D(2026, 11, 26))
    elif kind == "pay-refund":
        pay(w, members[who], 100_000, D(2026, 11, 24))
        r = client(w, "owner").post(f"{API}/org/memberships/{members[who].id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "Вернули"})
        assert r.status_code == 201, r.text
    else:
        leave_group(w.db, w.org.id, g.id, people[who].id, w.owner.id)
    return kind


@pytest.mark.parametrize("mode", list(MODES), ids=list(MODES))
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_property_an_unconfirmed_cancellation_moves_no_account_in_any_history_and_any_mode(pg_session, clock, mode, seed):
    """The matrix property on the loader of the database: the world WITH a waiting cancellation (the teacher, 24.11) and the world WITHOUT any cancellation have the SAME account of every student after the
    same random history of two facts (a payment / a pause / a payment and its refund / a leaving, on 25.11-26.11).  Hand: ``unconfirmed`` is no ``centre``: the ledger takes no cancellation from it,
    so the two worlds are one world in every billing mode (the package: a cancelled lesson is neutral anyway); the equality is exact, no rounding is involved (nothing is divided)."""
    seen = []
    for with_cancel in (True, False):
        w, g, people, members = world(pg_session, clock, students=4, price=200_000, **MODES[mode])
        clock.set("2026-11-24 12:00")
        if with_cancel:
            teacher_cancels(w, g)
            assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
        kinds = []
        for step in (1, 2):
            clock.set(f"2026-11-2{4 + step} 12:00")
            kinds.append(_history(seed, w, g, people, members, step))
        clock.set("2026-11-26 12:00")
        seen.append((kinds, accounts(w, members)))
    assert seen[0] == seen[1], seen


# ========================================================================================== the anchors of the bypass roads (П2, П14): the refusals that were are still there
def test_anchor_the_administrator_without_the_right_cancels_a_lesson_of_any_age_and_it_waits(pg_session, clock):
    """П2: the administrator (no money right) cancels the Monday 05.10 - 52 days old, no age check for him - the row is ``unconfirmed`` (not ``centre``), the nine accounts stay (hand: 0 / 0 / 0)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g, day="2026-10-05", who="admin")
    assert kind_in_db(w, g, day="2026-10-05") == ("cancelled", "unconfirmed") and accounts(w, members) == [(0, 0, 0)] * 9


def test_anchor_a_teacher_does_not_move_a_past_lesson(pg_session, clock):
    """П14: the teacher of the lesson cannot MOVE the Monday of 23.11 (3 days ago): 409 ``LESSON_ALREADY_STARTED``; nothing is written (no row, accounts 0)."""
    w, g, people, members = world(pg_session, clock)
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": MON, "lessonDate": PAST_MON, "newDate": "2026-11-30", "startTime": "16:00", "endTime": "17:30", "reason": "Перенос"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED", r.text
    assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9


def test_anchor_a_request_for_a_substitute_on_a_past_lesson_is_refused(pg_session, clock):
    """П14: «не смогу провести» on a lesson that has begun (23.11, past): 409 ``LESSON_ALREADY_STARTED`` (the request is a sentence about the FUTURE); no request is written."""
    w, g, people, members = world(pg_session, clock)
    r = client(w, "A").post(f"{API}/org/substitutions", json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": PAST_MON}], "reasonCode": "sick"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED", r.text
    assert kind_in_db(w, g) is None


def test_anchor_a_one_off_lesson_in_the_past_is_refused_for_the_teacher_and_the_administrator(pg_session, clock):
    """П14: a one-off lesson «instead of the regular one» is only for the future time: the teacher and the administrator both get 400 «Нельзя добавить разовый урок на прошедшую дату» for 23.11, and no row of the day appears."""
    w, g, people, members = world(pg_session, clock)
    for who in ("A", "admin"):
        r = client(w, who).post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": PAST_MON, "startTime": "15:00", "endTime": "16:30", "purpose": "extra"})
        assert r.status_code == 400 and "прошедшую дату" in r.text, (who, r.status_code, r.text)
    assert kind_in_db(w, g, day=PAST_MON, lesson=MON) is None and accounts(w, members) == [(0, 0, 0)] * 9


# ========================================================================================== the windows 1 and 30 x the access of a substitute and of a former teacher
@pytest.fixture
def d2(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world_ = build_d2(pg_session)
    grant_money_right(world_)
    return world_


@pytest.mark.parametrize("window, today, access", [
    # the substitute D has the lesson of Mon 26.10.  access = max(7, window) days after the day of the lesson, inclusive: 31.10 = 5 d, 02.11 = 7 d, 03.11 = 8 d, 09.11 = 14 d, 10.11 = 15 d, 25.11 = 30 d, 26.11 = 31 d.
    (1, "2026-10-31", True), (1, "2026-11-02", True), (1, "2026-11-03", False),            # window 1: the access stays 7 days
    (30, "2026-11-03", True), (30, "2026-11-25", True), (30, "2026-11-26", False),         # window 30: 30 days
    (14, "2026-11-09", True), (14, "2026-11-10", False),
])
def test_the_access_of_a_substitute_is_max_7_and_the_window(d2, clock, window, today, access):
    w = d2
    item = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Температура", suggested="D")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    assert put(w, teacher_lesson_window_days=window).status_code == 200
    clock.set(f"{today} 11:00")
    got = client(w, "D").get(f"{ORG}/groups/{w.G1.id}/lesson-access").status_code
    assert got == (200 if access else 403), (window, today, got)


@pytest.mark.parametrize("window, visible", [
    # A hands 9-А over to C on 21.10 and looks on Mon 26.10 09:00: access = max(7, window); his own lessons are Mon 12.10 (14 d) and Mon 19.10 (7 d); the cut is today - access, inclusive.
    (1, ["2026-10-19"]), (3, ["2026-10-19"]), (7, ["2026-10-19"]),                         # 7 d passes, 14 d does not
    (14, ["2026-10-12", "2026-10-19"]), (30, ["2026-10-12", "2026-10-19"]),
])
def test_the_journal_of_a_former_teacher_is_max_7_and_the_window(d2, clock, window, visible):
    w = d2
    assert put(w, teacher_lesson_window_days=window).status_code == 200
    r = client(w, "admin").put(f"{ORG}/groups/{w.G1.id}", json={"teacher_id": str(w.C.id)})
    assert r.status_code == 200, r.text
    clock.set("2026-10-26 09:00")
    j = client(w, "A").get(f"{ORG}/groups/{w.G1.id}/attendance", params={"month": "2026-10"})
    assert j.status_code == 200, j.text
    dates = sorted({x["instanceKey"].split("_")[0] for x in j.json()["lessons"] if x["instanceKey"].endswith("lesson-l1") and x["instanceKey"].split("_")[0] < "2026-10-26"})
    assert dates == visible, dates
