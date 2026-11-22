"""PAY-TAILS Б11 / Б11.8 step 8.0: the leaving as an EVENT of truncation (У1, У3, У4, У6, У7, У8), the correction of a payment that keeps the moment of the FIRST record (Б11.1) and the opening balance in the
replay (Б11.3).  PostgreSQL, through the routes of the product (LESSONS 135); every number is worked out by hand in the comment.  У2 and У5 are on the pure ledger (``test_pay_tails_b11_pure``): the routes cannot reach them.

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1.  October 2026: 14 lessons (600 000 x k / 14: 300 000 for 7, 257 000 for 6, 214 000 for 5, 386 000 for 9).  November: 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each."""
from datetime import date

from tests.money1b_helpers import add, fresh_student, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_fix_r3_pg import replace_it, restore_it, void_it
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import TTF, check, forgive, leave, led
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date


def om_at(w, fixed_today, joined, price=600_000):
    """A common month 600 000 and a student who joined on ``joined``; the clock stays on ``joined`` (the tests walk it forward)."""
    fixed_today(joined)
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(w.db, w.org, w.teacher, "Общий")
    give_group_price(w.db, g, price)
    set_schedule(w, TTF, g)
    ensure_billing(w, g, price=None)
    s = fresh_student(w, "Ученик ОМ")
    add(w, s, group=g, mode="add")
    return g, s


def on(fixed_today, w, s, g, day, what, *a, **kw):
    fixed_today(day)
    return what(w, s, g, *a, **kw)


# ---------------------------------------------------------------------------------------------------------------------------- У1, У6, У7: a pause that a leaving took off / cut
def november_with_forgiveness_and_money(world, fixed_today):
    # the student since 01.11; 02.11 a pause 24 - 30.11 (November 9 lessons = 450 000); 05.11 the whole 450 000 are forgiven; 10.11 the parent pays 600 000 (December).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 2))
    pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 450_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 10))
    check(world, s, g, D(2026, 11, 10), charged=450_000, debt=0, advance=600_000, used=450_000, granted=450_000)
    return g, s


def test_u1_a_leaving_that_takes_off_a_future_pause_keeps_the_forgiveness_and_the_whole_advance(world, fixed_today):
    # Before the leaving: debt 0, advance 600 000, used 450 000.  20.11 he leaves: the pause 24 - 30.11 begins after the day of the leaving and is taken off; November 01 - 20.11 is 9 lessons = 450 000 (the same).  The forgiveness
    # was entered when the pause was known: it covers 450 000 of the 450 000; the 600 000 are money that was not needed: advance (to give back) 600 000, used 450 000.  (The pause that was taken off had left the account: 150 000
    # of the payment fell on November and the advance was 450 000.)
    g, s = november_with_forgiveness_and_money(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=600_000, used=450_000, granted=450_000)


def test_u1_the_same_world_with_a_transfer_carries_the_whole_advance(world, fixed_today):
    # Three groups of one centre (every group has a lesson every day: a month of 30 days has 30 lessons): A 300 000.  The student since 01.11; 02.11 a pause 24 - 30.11 (23 lessons = 230 000); 05.11 230 000 forgiven; 10.11
    # 300 000 paid; 20.11 transferred to B: November 01 - 20.11 is 20 lessons = 200 000.  The forgiveness (230 000) is cut to 200 000 by the transfer, the 300 000 of money were not needed in A: all of them travel.
    from tests.test_money2_transfer_a_pg import groups3

    a, b, _ = groups3(world, fixed_today, D(2026, 11, 1))
    s = fresh_student(world, "Ученик перевода")
    add(world, s, group=a, mode="add")
    fixed_today(D(2026, 11, 2))
    pause_on(world, s, a, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 5))
    forgive(world, s, a, 230_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, a, amount=300_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 20))
    add(world, s, group=b, mode="transfer", from_group_id=str(a.id))
    L = led(world, s, a, D(2026, 11, 20))
    assert (L.charged, L.debt, L.written_off, L.transferred_out, L.advance) == (200_000, 0, 200_000, 300_000, 0), L
    assert led(world, s, b, D(2026, 11, 20)).transferred_in == 300_000


def pause_cut_world(world, fixed_today):
    # the student since 02.11 (the first lesson 03.11); 03.11 a pause 15 - 30.11 (November 6 lessons = 300 000); 05.11 300 000 forgiven (debt 0); 06.11 300 000 paid (all advance): advance 300 000, used 300 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 2))
    fixed_today(D(2026, 11, 3))
    pause_on(world, s, g, "2026-11-15", "2026-11-30")
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 6))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 6))
    check(world, s, g, D(2026, 11, 6), charged=300_000, debt=0, advance=300_000, used=300_000, granted=300_000)
    return g, s


def test_u7_a_pause_cut_by_the_leaving_is_not_an_extension_that_cuts_the_forgiveness(world, fixed_today):
    # 20.11 he leaves: the pause 15 - 30.11 is cut to 15 - 20.11; November 02 - 20.11 = 6 lessons (3 5 6 10 12 13: the lessons 17 19 20 are in the pause) = 300 000, as before.  Nothing was lost: advance 300 000, used 300 000.
    # (The cut of the pause was read as an entry of a new, SHORTER pause: the place of November was 450 000 and the money took 150 000 of it: 150 000 / 150 000.)
    g, s = pause_cut_world(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 20), charged=300_000, debt=0, advance=300_000, used=300_000, granted=300_000)


def test_u6_the_leaving_cancelled_the_same_day_gives_the_account_of_before_the_leaving_a_pause_that_was_cut(world, fixed_today):
    # The world of У7; the leaving of 20.11 is cancelled the same day (the return on the day of the leaving).  The cut of the pause and its restoration are not forms of the pause (Б11.8 п. 2): the pause is 15 - 30.11 as
    # before: November 300 000, advance 300 000, used 300 000 - the account of before the leaving.
    g, s = pause_cut_world(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    add(world, s, group=g, mode="add")
    check(world, s, g, D(2026, 11, 20), charged=300_000, debt=0, advance=300_000, used=300_000, granted=300_000)


def test_u6_the_leaving_cancelled_the_same_day_gives_the_account_of_before_the_leaving_a_pause_that_was_taken_off(world, fixed_today):
    # The world of У1; the leaving of 20.11 takes off the pause 24 - 30.11, the cancel gives it back: the account of 10.11 - debt 0, advance 600 000, used 450 000.
    g, s = november_with_forgiveness_and_money(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    add(world, s, group=g, mode="add")
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=600_000, used=450_000, granted=450_000)


# ---------------------------------------------------------------------------------------------------------------------------- У3, У4: the records entered BEFORE the leaving (С1 - С3 do not change)
def october_leaver(world, fixed_today, steps, leave_on):
    """The student since 01.10; ``steps`` = [(day, what, amount)]; the leaving on ``leave_on``."""
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    for day, what, amount in steps:
        fixed_today(day)
        if what == "pay":
            pay(world, s, g, amount=amount, paid_on=day)
        else:
            forgive(world, s, g, amount)
    fixed_today(leave_on)
    leave(world, g, s)
    return g, s


def test_u3_money_and_a_forgiveness_entered_before_the_leaving_money_first_whatever_the_order(world, fixed_today):
    # 300 000 paid and 300 000 forgiven (any order), then the leaving on 13.10: October 01 - 13.10 = 6 lessons = 257 000.  The money pays October (257 000), the forgiveness is not needed (used 0), 43 000 of the money
    # are the advance: debt 0, advance 43 000, used 0.
    g, s = october_leaver(world, fixed_today, [(D(2026, 10, 1), "pay", 300_000), (D(2026, 10, 5), "forgive", 300_000)], D(2026, 10, 13))
    check(world, s, g, D(2026, 10, 13), charged=257_000, debt=0, advance=43_000, used=0, granted=300_000)


def test_u3_the_other_order_the_forgiveness_first(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    fixed_today(D(2026, 10, 1))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 10, 5))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 10, 5))
    fixed_today(D(2026, 10, 13))
    leave(world, g, s)
    check(world, s, g, D(2026, 10, 13), charged=257_000, debt=0, advance=43_000, used=0, granted=300_000)


def test_u4_a_forgiven_period_is_used_by_the_cost_of_the_cut_period(world, fixed_today):
    # 05.10 the whole October (600 000) is forgiven; the leaving on 13.10: October 257 000: used 257 000, advance 0, debt 0 (the rest of the forgiveness - 343 000 - is thrown away).
    g, s = october_leaver(world, fixed_today, [(D(2026, 10, 5), "forgive", 600_000)], D(2026, 10, 13))
    check(world, s, g, D(2026, 10, 13), charged=257_000, debt=0, advance=0, used=257_000, granted=600_000)


def test_u4_money_and_a_forgiveness_inside_the_cost_of_the_cut_period(world, fixed_today):
    # 200 000 paid (01.10), 400 000 forgiven (05.10); the leaving on 15.10: October 7 lessons = 300 000: money 200 000 + the forgiveness 100 000: used 100 000, advance 0, debt 0.
    g, s = october_leaver(world, fixed_today, [(D(2026, 10, 1), "pay", 200_000), (D(2026, 10, 5), "forgive", 400_000)], D(2026, 10, 15))
    check(world, s, g, D(2026, 10, 15), charged=300_000, debt=0, advance=0, used=100_000, granted=400_000)


# ---------------------------------------------------------------------------------------------------------------------------- У8: the reachable path of the finding 6 (a return to the group)
def test_u8_forgiven_at_the_settlement_of_a_leaver_then_returned_and_paid_november_the_payment_pays_november(world, fixed_today):
    # The student since 01.10; 09.10 he leaves (October 01 - 09.10 = 5 lessons = 214 000); 12.10 the 214 000 are forgiven (debt 0); 01.11 he comes back (a new piece of the stay); 03.11 (the first lesson: November has begun) the parent pays November 600 000.
    # October is closed by the forgiveness (used 214 000), November by the money: debt 0, advance 0; charged 214 000 + 600 000 = 814 000.  (The payment lay on the free NOMINAL room of October and pushed the forgiveness
    # out: debt 214 000, used 0 - the family paid November and still owed.)
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    fixed_today(D(2026, 10, 9))
    leave(world, g, s)
    fixed_today(D(2026, 10, 12))
    forgive(world, s, g, 214_000)
    fixed_today(D(2026, 11, 1))
    add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 3))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    check(world, s, g, D(2026, 11, 3), charged=814_000, debt=0, advance=0, used=214_000, granted=214_000)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.1: a correction keeps the moment of the first record
def october_payment_before_the_pause(world, fixed_today):
    # the student since 01.10; 05.10 300 000 are forgiven (October 600 000); 10.10 a payment of 300 000; 12.10 a pause 22 - 31.10 (October 386 000: the forgiveness is cut by 214 000, alive 86 000).
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    fixed_today(D(2026, 10, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 10, 10))
    p = pay(world, s, g, amount=300_000, paid_on=D(2026, 10, 10))["payment"]
    return g, s, p


def test_b11_1_a_correction_without_a_change_does_not_change_the_account(world, fixed_today):
    # money 300 000 + the forgiveness 86 000 = October 386 000: debt 0, advance 0, used 86 000.  20.10 the payment is «corrected» (the same sum and date): the same account.
    g, s, p = october_payment_before_the_pause(world, fixed_today)
    fixed_today(D(2026, 10, 12))
    pause_on(world, s, g, "2026-10-22", "2026-10-31")
    check(world, s, g, D(2026, 10, 12), charged=386_000, debt=0, advance=0, used=86_000, granted=300_000)
    fixed_today(D(2026, 10, 20))
    replace_it(world, p, 300_000)
    check(world, s, g, D(2026, 10, 20), charged=386_000, debt=0, advance=0, used=86_000, granted=300_000)


def test_b11_1_a_correction_of_the_sum_keeps_the_moment_of_the_first_record(world, fixed_today):
    # The same world.  The sum becomes 100 000 (the payment was 100 000 on 10.10): the forgiveness is cut by 14 000: 100 000 + 286 000 = 386 000: advance 0, used 286 000.   400 000: advance 100 000, used 86 000.
    # 500 000: advance 200 000, used 86 000 (500 000 + 86 000 - 386 000).  Each correction is of the payment that stands now (the chain of the corrections keeps the moment of the first).
    g, s, p = october_payment_before_the_pause(world, fixed_today)
    fixed_today(D(2026, 10, 12))
    pause_on(world, s, g, "2026-10-22", "2026-10-31")
    fixed_today(D(2026, 10, 20))
    for amount, advance, used in ((100_000, 0, 286_000), (400_000, 100_000, 86_000), (500_000, 200_000, 86_000)):
        p = replace_it(world, p, amount)["payment"]
        check(world, s, g, D(2026, 10, 20), charged=386_000, debt=0, advance=advance, used=used, granted=300_000)


def test_b11_1_a_cancelled_payment_restored_after_the_pause_is_the_account_of_before_the_cancel(world, fixed_today):
    # The payment is cancelled on 11.10 (before the pause) and RESTORED on 20.10 (after it): the restoration is the same payment of 10.10: money 300 000 + the forgiveness 86 000: advance 0, used 86 000.
    g, s, p = october_payment_before_the_pause(world, fixed_today)
    fixed_today(D(2026, 10, 11))
    void_it(world, p)
    fixed_today(D(2026, 10, 12))
    pause_on(world, s, g, "2026-10-22", "2026-10-31")
    fixed_today(D(2026, 10, 20))
    restore_it(world, p)
    check(world, s, g, D(2026, 10, 20), charged=386_000, debt=0, advance=0, used=86_000, granted=300_000)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.3: the opening balance in the replay
def opening_world(world, fixed_today, pause_first):
    # the student since 01.10; the opening balance: owed 200 000 on 30.09.  03.10 the parent pays 200 000 (the opening debt is closed); 05.10 300 000 of October (600 000) are forgiven; a pause 22 - 31.10 (October 386 000)
    # is entered 20.10 (the pause AFTER) or 02.10 (BEFORE).  The money lies on the opening debt: it pushes nothing out of October: used 300 000, the debt is 386 000 - 300 000 = 86 000.  Charged 200 000 + 386 000 = 586 000.
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    m = membership(world, s, g)
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"debt_amount": 200_000, "debt_on": "2026-09-30"})
    assert r.status_code == 200, r.text
    if pause_first:
        fixed_today(D(2026, 10, 2))
        pause_on(world, s, g, "2026-10-22", "2026-10-31")
    fixed_today(D(2026, 10, 3))
    pay(world, s, g, amount=200_000, paid_on=D(2026, 10, 3))
    fixed_today(D(2026, 10, 5))
    forgive(world, s, g, 300_000)
    if not pause_first:
        fixed_today(D(2026, 10, 20))
        pause_on(world, s, g, "2026-10-22", "2026-10-31")
    fixed_today(D(2026, 10, 25))
    check(world, s, g, D(2026, 10, 25), charged=586_000, debt=86_000, advance=0, used=300_000, granted=300_000)


def test_b11_3_the_pause_entered_after_the_forgiveness_leaves_the_opening_debt_paid_and_the_debt_86_000(world, fixed_today):
    opening_world(world, fixed_today, pause_first=False)


def test_b11_3_the_pause_entered_before_the_forgiveness_gives_the_same_account(world, fixed_today):
    opening_world(world, fixed_today, pause_first=True)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.12 п. 1: leaving, cancel, a payment, leaving again - the same day
def relapse_world(world, fixed_today, marked):
    # ОМ 600 000, Tue / Thu / Fri; the student since 01.11; 05.11 300 000 are forgiven (November: debt 300 000).  20.11: he leaves, is taken back the same day (the cancel of the leaving), the parent pays 300 000
    # (inside the debt), and he leaves AGAIN.  ``marked``: a counted mark of the lesson of 20.11 (Friday).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    add(world, s, group=g, mode="add")
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 20))
    if marked:
        from tests.money1b_helpers import hold_lessons

        hold_lessons(world, s, [D(2026, 11, 20)], group=g)
    leave(world, g, s)
    return g, s


def test_b11_12_leave_cancel_payment_leave_again_the_forgiveness_is_cut_not_turned_into_an_advance(world, fixed_today):
    # November 01 - 20.11 = 9 lessons (3 5 6 10 12 13 17 19 20) = 450 000.  The forgiveness 300 000 was entered when the whole month stood; the payment 300 000 was entered while he was in the group - BEFORE the
    # second leaving.  The world of the second leaving: the month costs 450 000, the money 300 000 and the forgiveness cut to 150 000: used 150 000, debt 0, advance 0 (nothing to give back).
    # (The piece opened by the cancel and closed the same day has no day and fell out; the leaving of the first piece was the EARLIER one: the payment counted as made AFTER the leaving and the forgiveness 300 000
    # was left whole: used 300 000, advance 150 000.)
    g, s = relapse_world(world, fixed_today, marked=False)
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
    assert led(world, s, g, D(2026, 11, 20)).refundable == 0


def test_b11_12_the_same_world_with_a_counted_mark_on_the_day_of_the_leaving_is_the_same(world, fixed_today):
    # The guard: with a counted mark of the lesson of 20.11 the second piece is a real stay of one day and does not fall out: the same numbers.
    g, s = relapse_world(world, fixed_today, marked=True)
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
