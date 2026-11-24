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
