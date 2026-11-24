"""PAY-TAILS Б11.35 / Б11.36, step 8.14.8: the new pause after a return does not depend on the old tail (В2).  PostgreSQL, through the routes of the product (LESSONS 135).  The FOUR numbers of lesson 168 at every step
(the probe, ``impact`` and ``row`` of the answer, the account after) and the charge / the used forgiveness.

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1; November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each.
Common steps: 05.11 paid 150 000; 09.11 the pause A; 19.11 the leaving (A -> 17 - 19, or a FUTURE A 24 - 30 is taken off whole); 21.11 an ordinary return; 22.11 forgiven 300 000, THEN a new pause B 24 - 30, THEN paid 100 000;
23.11 the human changes B.
Hand counts (the number of the lessons in the world, x 50 000):
- A 17 - 30 cut to 17 - 19: the stay 01 - 19 without 17, 19 = 3 5 6 10 12 13 (6) + 24 26 27 (3) = 450 000.  B 24 - 30 takes 24 26 27: 300 000 (debt 450 000 - 150 000 paid = 300 000 at 04:00: the forgiveness 300 000 covers it;
  after B the debt is 150 000: used 150 000), the payment 100 000 is an advance.  B taken off: 450 000, paid 150 000 + forgiven 300 000 (the whole of it: the debt is 300 000) -> used 300 000, debt 0, the payment 100 000 is an advance.
  B shortened to 24 - 24: 8 lessons = 400 000, used 250 000, the payment 100 000 is an advance.
- a FUTURE A 24 - 30 taken off by the leaving (it never was): the stay 01 - 19 (3 5 6 10 12 13 17 19 = 8) + 24 26 27 = 11 x 50 000 = 550 000; B taken off: paid 150 000 + 100 000, used 300 000 -> 550 000, debt 0, advance 0; B -> 24 - 24:
  500 000, used 300 000 (the forgiveness is older than the payment), advance 50 000.
The ORACLE of each: the same leaving and return, A entered at once as 17 - 19 (no tail), or no A at all (a future A the leaving took off): the numbers are the same."""
from datetime import date

from tests.money1b_helpers import add
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b13_pg import edit_pause
from tests.test_pay_tails_b14_pg import new_pause, void_pause
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import check, forgive, leave, led
from tests.test_payments_service_pg import pay
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def v2_world(world, fixed_today, kind, oracle):
    """The common steps up to the payment of 22.11; ``kind`` 'tail' (A 17 - 30, cut) or 'future' (A 24 - 30 taken off whole); ``oracle``: the same without the old tail.  Returns ``(g, s, b_id)``."""
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    if kind == "tail":
        pause_on(world, s, g, "2026-11-17", "2026-11-19" if oracle else "2026-11-30")
    elif not oracle:
        pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    leave(world, g, s)
    fixed_today(D(2026, 11, 21))
    add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 22))
    forgive(world, s, g, 300_000)
    if kind == "tail":
        b_id = new_pause(world, s, g, D(2026, 11, 22), "2026-11-24", "2026-11-30", debt=0, advance=0, charged=300_000, used=150_000, granted=300_000)
    else:
        b_id = new_pause(world, s, g, D(2026, 11, 22), "2026-11-24", "2026-11-30", debt=0, advance=0, charged=400_000, used=250_000, granted=300_000)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 22))
    check(world, s, g, D(2026, 11, 22), charged=300_000 if kind == "tail" else 400_000, debt=0, advance=100_000, used=150_000 if kind == "tail" else 250_000, granted=300_000)
    fixed_today(D(2026, 11, 23))
    return g, s, b_id


def b_removed(world, fixed_today, kind, oracle):
    g, s, b_id = v2_world(world, fixed_today, kind, oracle)
    if kind == "tail":
        void_pause(world, s, g, D(2026, 11, 23), b_id, debt=0, advance=100_000, charged=450_000, used=300_000, granted=300_000)
    else:
        void_pause(world, s, g, D(2026, 11, 23), b_id, debt=0, advance=0, charged=550_000, used=300_000, granted=300_000)


def b_shortened(world, fixed_today, kind, oracle):
    g, s, b_id = v2_world(world, fixed_today, kind, oracle)
    if kind == "tail":
        edit_pause(world, s, g, b_id, D(2026, 11, 23), "2026-11-24", "2026-11-24", debt=0, advance=100_000, charged=400_000, used=250_000, granted=300_000)
    else:
        edit_pause(world, s, g, b_id, D(2026, 11, 23), "2026-11-24", "2026-11-24", debt=0, advance=50_000, charged=500_000, used=300_000, granted=300_000)


# the table of Б11.35, rows 4 and 5 (the tail of a cut pause) and the same class through the pause a leaving took off whole
def test_b11_35_v2_row4_the_new_pause_taken_off_gives_the_account_of_a_world_without_the_old_tail(world, fixed_today):
    b_removed(world, fixed_today, "tail", False)


def test_b11_35_v2_row4_oracle_the_same_leaving_and_return_with_the_pause_entered_17_19_at_once(world, fixed_today):
    b_removed(world, fixed_today, "tail", True)


def test_b11_35_v2_row5_the_new_pause_shortened_gives_the_account_of_a_world_without_the_old_tail(world, fixed_today):
    b_shortened(world, fixed_today, "tail", False)


def test_b11_35_v2_row5_oracle_the_same_leaving_and_return_with_the_pause_entered_17_19_at_once(world, fixed_today):
    b_shortened(world, fixed_today, "tail", True)


def test_b11_36_v2_future_pause_taken_off_by_the_leaving_the_new_pause_taken_off(world, fixed_today):
    b_removed(world, fixed_today, "future", False)


def test_b11_36_v2_future_pause_oracle_there_was_no_old_pause(world, fixed_today):
    b_removed(world, fixed_today, "future", True)


def test_b11_36_v2_future_pause_taken_off_by_the_leaving_the_new_pause_shortened(world, fixed_today):
    b_shortened(world, fixed_today, "future", False)


def test_b11_36_v2_future_pause_oracle_there_was_no_old_pause_shortened(world, fixed_today):
    b_shortened(world, fixed_today, "future", True)


# ---------------------------------------------------------------------------------------------------------------------------- В1: the cancel of the leaving and the pause a human took off after the cut
V1_AFTER_NEW_PAUSE = dict(debt=0, advance=300_000, charged=300_000, used=150_000, granted=300_000)


def v1_world(world, fixed_today, *, through="leave"):
    """05.11 paid 150 000; 09.11 the pause 17 - 30; 19.11 the leaving (or the archive of the student); the human takes the pause off; the student is back the same day (the leaving is cancelled); forgiven 300 000; paid 300 000; a NEW pause 17 - 30.
    Hand count: 12 lessons x 50 000 = 600 000 (the pause 17 - 30 takes 6: 300 000); paid 150 000, the debt 150 000 is covered by the forgiveness (used 150 000), the payment 300 000 is an advance: 300 000 / 0 / 150 000 / 300 000 / 300 000."""
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    a_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    if through == "leave":
        leave(world, g, s)
    else:
        r = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
        assert r.status_code == 200, r.text
    void_pause(world, s, g, D(2026, 11, 19), a_id, debt=250_000, advance=0, charged=400_000, used=0, granted=0)
    if through == "leave":
        back = add(world, s, group=g, mode="add")["leave_undo"]
    else:
        r = owner(world).post(f"{API}/org/students/{s.id}/restore", json={"group_ids": [str(g.id)]})
        assert r.status_code == 200, r.text
        back = r.json()
    return g, s, a_id, back


def finish_v1(world, g, s):
    forgive(world, s, g, 300_000)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 19))
    new_pause(world, s, g, D(2026, 11, 19), "2026-11-17", "2026-11-30", **V1_AFTER_NEW_PAUSE)
    check(world, s, g, D(2026, 11, 19), charged=300_000, debt=0, advance=300_000, used=150_000, granted=300_000)
    assert led(world, s, g, D(2026, 11, 19)).refundable == 300_000


def test_b11_35_v1_the_cancel_of_the_leaving_after_the_human_took_the_pause_off_a_new_pause_on_the_same_days(world, fixed_today):
    g, s, a_id, back = v1_world(world, fixed_today)
    assert back["undone"] is True, back
    finish_v1(world, g, s)


def lines_of(world, pause_id):
    """The lines of the journal of the pause, with the snapshot: ``[(action, snapshot)]`` in time."""
    from src.models.auth import GroupAudit
    world.db.expire_all()
    return [(a.action, dict(a.snapshot)) for a in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id).order_by(GroupAudit.created_at, GroupAudit.id) if (a.snapshot or {}).get("pause_id") == pause_id]


def test_b11_36_4_the_cancel_writes_the_line_for_the_pause_a_human_took_off_it_is_no_form_the_journal_stays_sound_the_second_cancel_and_the_second_leaving_pass_it(world, fixed_today):
    from datetime import datetime
    from uuid import UUID
    from src.auth.ledger_service import pause_journal
    from src.auth.logica_service import PauseService
    from src.models.auth import MembershipPause, StudentGroup
    g, s, a_id, back = v1_world(world, fixed_today)
    assert back["undone"] is True and back["restored"] == [], back                                   # nothing is given back: the pause does not exist
    lines = lines_of(world, a_id)
    assert [(a, x.get("via"), x.get("was")) for a, x in lines] == [
        ("membership_pause_set", None, None), ("membership_pause_set", "leave", ["2026-11-17", "2026-11-30"]), ("membership_pause_void", None, None), ("membership_pause_set", "leave_undone", None)], lines
    last = lines[-1][1]
    assert (last["first_day"], last["last_day"], last["was"], last["restored"], last["via"]) == ("2026-11-17", "2026-11-19", None, False, "leave_undone"), last
    row = world.db.get(MembershipPause, UUID(a_id))
    assert (row.first_day, row.last_day) == (D(2026, 11, 17), D(2026, 11, 19)) and row.voided_at is not None and row.void_reason == "Передумали"          # the row is as the human left it
    journal = pause_journal(world.db, str(world.org.id), row)
    assert journal.cut is None and journal.sound and len(journal.forms) == 1 and journal.forms[0][1:] == (D(2026, 11, 17), D(2026, 11, 30)), journal   # the line is no form of a human
    membership_row = world.db.get(StudentGroup, membership(world, s, g).id)
    for _ in range(2):                                                                                   # the second cancel of the same leaving: nothing is written
        assert PauseService(world.db).restore_after_leave(str(world.org.id), membership_row, datetime(2000, 1, 1), str(world.owner.id)) == []
    assert lines_of(world, a_id) == lines
    leave(world, g, s)                                                                                   # the second leaving does not touch the pause either (it is not live)
    assert lines_of(world, a_id) == lines
    back = add(world, s, group=g, mode="add")["leave_undo"]
    assert back["undone"] is True and lines_of(world, a_id) == lines, (back, lines_of(world, a_id))


def test_b11_36_4_the_line_is_shown_nowhere_the_journal_of_decisions_has_no_line_of_a_pause(world, fixed_today):
    # the journal of the decisions (``/org/payments/decisions``) reads only the lines of the decisions and the carrying of a pause by a transfer (``payments_income.EVENT_KINDS``): a line of a pause with ``via = leave_undone`` is in none
    # of them, so the text of a line the journal of a group would show does not exist (the neighbour lines of the same ``via`` - a pause given back - are shown nowhere either).
    v1_world(world, fixed_today)
    body = owner(world).get(f"{API}/org/payments/decisions", params={"from": "2026-11-01", "to": "2026-11-30", "size": 100}).json()
    assert all(r["kind"] not in ("membership_pause_set", "membership_pause_void") for r in body["rows"]), [r["kind"] for r in body["rows"]]


# ---------------------------------------------------------------------------------------------------------------------------- Б11.36 п. 8: the guard of the row 8 (the leaving is NOT cancelled)
def row8_world(world, fixed_today, *, with_pause):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    a_id = pause_on(world, s, g, "2026-11-17", "2026-11-30") if with_pause else None
    fixed_today(D(2026, 11, 19))
    leave(world, g, s)
    return g, s, a_id


def test_b11_36_8_a_human_takes_off_the_pause_of_the_student_who_left_four_numbers_one_set_the_account_is_that_of_a_world_without_the_pause(world, fixed_today):
    # 05.11 paid 150 000; 09.11 the pause 17 - 30; 19.11 the leaving (the cut 17 - 19: lessons 3 5 6 10 12 13 = 300 000, debt 150 000).  The human takes the pause off: the lessons 17 and 19 are charged (8 x 50 000 = 400 000), the debt is 250 000 -
    # the one who left loses part of the money he was owed back (nothing is back here: he owes more): the window of the take-off says it before the confirmation (the probe).  The oracle: the same without the pause.
    g, s, a_id = row8_world(world, fixed_today, with_pause=True)
    check(world, s, g, D(2026, 11, 19), charged=300_000, debt=150_000, advance=0, used=0, granted=0)
    void_pause(world, s, g, D(2026, 11, 19), a_id, debt=250_000, advance=0, charged=400_000, used=0, granted=0)
    check(world, s, g, D(2026, 11, 19), charged=400_000, debt=250_000, advance=0, used=0, granted=0)


def test_b11_36_8_oracle_the_same_leaving_without_any_pause(world, fixed_today):
    g, s, _ = row8_world(world, fixed_today, with_pause=False)
    check(world, s, g, D(2026, 11, 19), charged=400_000, debt=250_000, advance=0, used=0, granted=0)


def test_b11_36_9_the_same_world_through_the_archive_of_the_student_and_the_return_from_the_archive_the_same_day(world, fixed_today):
    g, s, a_id, back = v1_world(world, fixed_today, through="archive")
    last = lines_of(world, a_id)[-1][1]
    assert (last["via"], last["was"], last["restored"], last["first_day"], last["last_day"]) == ("leave_undone", None, False, "2026-11-17", "2026-11-19"), last     # the return from the archive reaches the same cancel
    finish_v1(world, g, s)
