"""PAY-TAILS Б11 / Б11.8 step 8.0: the named worlds of the leaving as an EVENT of truncation, of the correction of a payment and of the opening balance, on the pure ledger (numbers by hand in the comments).

The common month 600 000, lessons Tue / Thu / Fri.  October 2026 has 14 lessons (Thu 1 8 15 22 29, Fri 2 9 16 23 30, Tue 6 13 20 27): 600 000 x k / 14 -> 300 000 for 7 (01 - 15.10), 386 000 for 9 (a pause 22 - 31.10 takes 5).
Every fact is entered at a MOMENT (``tools.at(day, minute)``).  У2 and У5 are reachable only on the pure ledger (the routes of the product do not allow them: a student who has left pays only inside his debt; the leaving always closes the
stay with the moment NOW) - the marks say so.  The same worlds on the routes of the product are in ``test_pay_tails_b11_pg``."""
from dataclasses import replace

from src.auth import billing_ledger as bl
from tests import pay_tails_b7_tools as tools
from tests.pay2_ledger_helpers import D, flat, make, pause

PAY, WO = bl.PAYMENT, bl.WRITEOFF
credit = tools.mk_credit


def common(today, stays, credits, pauses=()):
    return replace(make(today=today, stays=stays, price_at=flat(600_000), regimes=[tools.FIXED1], start=D(2026, 9, 1), lesson_days=tools.TTF, credits=credits), pauses=tuple(pauses))


def figures(inp):
    L = bl.run_ledger(inp)
    assert L.reconciles and not L.flags, L.flags
    return dict(charged=L.charged, debt=L.debt, advance=L.advance, used=L.written_off)


def test_u2_unreachable_by_the_routes_a_forgiveness_then_a_payment_after_the_leaving_the_payment_is_the_advance_to_give_back():
    # (pure ledger: the route of a payment takes nothing from a student who has left above his debt.)  The student stayed 01 - 15.10 (October 7 lessons = 300 000), left on 15.10.  16.10 the whole 300 000 are forgiven (debt 0).
    # 18.10 a payment of 300 000 DATED 18.10: October is closed by the forgiveness, the payment is the advance: charged 300 000, debt 0, advance 300 000, used 300 000; the limit of a refund is 300 000.
    # The same payment DATED 10.10 (before the date of the forgiveness): by the order of the dates the money comes first and pays October, the forgiveness is not needed: advance 0, used 0.
    stay = [tools.mk_stay(D(2026, 10, 1), D(2026, 10, 15), None, tools.at(D(2026, 10, 15), 700))]
    late = common(D(2026, 10, 18), stay, [credit("w", WO, D(2026, 10, 16), D(2026, 10, 16), 10, 300_000), credit("p", PAY, D(2026, 10, 18), D(2026, 10, 18), 20, 300_000)])
    assert figures(late) == dict(charged=300_000, debt=0, advance=300_000, used=300_000)
    assert bl.refundable_at(late, D(2026, 10, 18)) == 300_000 and bl.refund_limit(late, D(2026, 10, 18)).amount == 300_000
    early = common(D(2026, 10, 18), stay, [credit("w", WO, D(2026, 10, 16), D(2026, 10, 16), 10, 300_000), credit("p", PAY, D(2026, 10, 10), D(2026, 10, 18), 20, 300_000)])
    assert figures(early) == dict(charged=300_000, debt=0, advance=0, used=0)


def test_u5_unreachable_by_the_routes_a_leaving_entered_later_with_an_earlier_day_leaves_no_november():
    # (pure ledger: all three ways to leave - the leaving, the transfer, the archive of the student - close the stay with the moment NOW.)  October is paid (600 000, 01.10).  05.11 the whole November (600 000) is forgiven:
    # on that day the student is in the group, November has begun.  20.11 a leaving is entered with the day 31.10: there is no November: the forgiveness is cut by the replay of the leaving: used 0, debt 0, advance 0.
    stay = [tools.mk_stay(D(2026, 10, 1), D(2026, 10, 31), None, tools.at(D(2026, 11, 20), 10))]
    inp = common(D(2026, 11, 25), stay, [credit("p", PAY, D(2026, 10, 1), D(2026, 10, 1), 10, 600_000), credit("w", WO, D(2026, 11, 5), D(2026, 11, 5), 10, 600_000)])
    assert figures(inp) == dict(charged=600_000, debt=0, advance=0, used=0)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.1: the correction of a payment keeps the moment of the first record
def october_world():
    # 05.10 300 000 are forgiven (October 600 000: debt 300 000 left); 10.10 a payment of 300 000; 12.10 a pause 22 - 31.10 is entered (October 386 000: 214 000 cut off the forgiveness: alive 86 000).
    return common(D(2026, 10, 20), [bl.Stay(D(2026, 10, 1))],
                  [credit("w", WO, D(2026, 10, 5), D(2026, 10, 5), 10, 300_000), credit("p", PAY, D(2026, 10, 10), D(2026, 10, 10), 10, 300_000)],
                  [pause(D(2026, 10, 22), D(2026, 10, 31), D(2026, 10, 12), 10)])


def corrected(amount, kind="amount"):
    return tools.apply_correct(october_world(), tools.Correct("p", kind, amount))


def test_b11_1_the_world_before_the_correction():
    # money 300 000 + the forgiveness 86 000 = October 386 000: debt 0, advance 0, used 86 000.
    assert figures(october_world()) == dict(charged=386_000, debt=0, advance=0, used=86_000)


def test_b11_1_a_correction_without_a_change_changes_nothing():
    assert figures(corrected(300_000, "same")) == figures(october_world())
    assert figures(corrected(300_000, "restore")) == figures(october_world())


def test_b11_1_a_correction_of_the_sum_down_to_100_000():
    # the payment is 100 000 at the moment of the FIRST record (10.10): the forgiveness 300 000 loses 14 000 (alive 286 000): 100 000 + 286 000 = 386 000: debt 0, advance 0, used 286 000.
    assert figures(corrected(100_000)) == dict(charged=386_000, debt=0, advance=0, used=286_000)


def test_b11_1_a_correction_of_the_sum_up_to_400_000():
    # 400 000 of money lie where they lay: October 300 000 (the forgiveness 86 000 takes the rest of its room), 100 000 go beyond: advance 100 000, used 86 000.
    assert figures(corrected(400_000)) == dict(charged=386_000, debt=0, advance=100_000, used=86_000)


def test_b11_1_a_correction_of_the_sum_up_to_500_000():
    # 500 000 + 86 000 - 386 000 = 200 000 of advance (money); the forgiveness of October stays 86 000 (the 200 000 of the payment lie on the next period, they push nothing out).
    assert figures(corrected(500_000)) == dict(charged=386_000, debt=0, advance=200_000, used=86_000)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.3: the opening balance has a place in every world
def opening_world(order):
    # an opening debt 200 000 (30.09) is paid by 200 000 (03.10); 05.10 300 000 of October (600 000) are forgiven; a pause 22 - 31.10 (October 386 000) is entered 20.10 (order "after") or 02.10 (order "before").
    entered = D(2026, 10, 20) if order == "after" else D(2026, 10, 2)
    inp = common(D(2026, 10, 25), [bl.Stay(D(2026, 10, 1))],
                 [credit("o", PAY, D(2026, 10, 3), D(2026, 10, 3), 10, 200_000), credit("w", WO, D(2026, 10, 5), D(2026, 10, 5), 10, 300_000)],
                 [pause(D(2026, 10, 22), D(2026, 10, 31), entered, 10)])
    return replace(inp, opening=bl.Opening(None, 200_000, D(2026, 9, 30)))


def test_b11_3_the_money_that_closed_the_opening_debt_does_not_push_out_the_forgiveness_of_october_whatever_the_order():
    # the opening debt is closed by its own 200 000; the forgiveness of 300 000 lies on October (room 386 000 after the pause): used 300 000; the rest of October 86 000 is the debt.  Charged 200 000 + 386 000 = 586 000.
    for order in ("after", "before"):
        assert figures(opening_world(order)) == dict(charged=586_000, debt=86_000, advance=0, used=300_000), order
