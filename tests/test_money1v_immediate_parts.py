"""Money-1 pack V0 (pure ledger, no database): «К оплате сразу» is told by the periods it is made of.

The owner saw «489 000 сум (за 10 уроков)» at the price 400 000 and did not understand the sum: 89 000 for October (2 lessons of 9, from 27.10) + 400 000 for the whole of November.
``parts`` is read from the SAME segments of the same ledger (one number - one function, LESSONS 104): the amounts add up to ``amount``, the lessons to ``lessons``."""
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import billing_start as bs
from tests.pay2_ledger_helpers import D, flat, make, weekly

TUE_FRI = weekly((1, 4))          # October 2026: Tue 6 13 20 27, Fri 2 9 16 23 30 = 9 lessons; November: Tue 3 10 17 24, Fri 6 13 20 27 = 8
FIRST_OF_MONTH = bl.Regime(anchor="fixed_day", billing_day=1)
NBSP_FREE = "–"


def charge(stays, today, *, price=400_000, regime=FIRST_OF_MONTH):
    inp = make(today=today, stays=stays, price_at=flat(price, D(2026, 1, 1)), start=D(2026, 9, 1), regimes=[regime], lesson_days=TUE_FRI)
    return bs.immediate_from_ledger(bl.run_ledger(inp), inp.lesson_days)


def test_v0_27_10_at_400000_is_89000_for_2_of_9_lessons_and_the_whole_of_november():
    got = charge([bl.Stay(D(2026, 10, 27))], D(2026, 11, 2))
    assert got["parts"] == [
        {"label": "октябрь", "period_start": "2026-10-01", "from": "2026-10-27", "amount": 89_000, "lessons": 2, "lessons_in_period": 9, "whole": False},
        {"label": "ноябрь", "period_start": "2026-11-01", "from": None, "amount": 400_000, "lessons": 8, "lessons_in_period": None, "whole": True}]
    assert got["amount"] == 489_000 and got["lessons"] == 10                              # the old fields mean what they meant
    assert sum(p["amount"] for p in got["parts"]) == got["amount"] and sum(p["lessons"] for p in got["parts"]) == got["lessons"]
    assert got["text"] == "К оплате сразу: 489 000 сум (за 10 уроков)"


def test_v0_the_payment_day_the_6th_names_the_period_by_its_days_not_by_a_month():
    got = charge([bl.Stay(D(2026, 10, 6))], D(2026, 11, 10), regime=bl.Regime())          # «свой месяц»: 06.10-05.11 and 06.11-05.12 have begun
    assert [(p["label"], p["amount"], p["whole"], p["from"]) for p in got["parts"]] == [
        ("06.10–05.11", 400_000, True, None), ("06.11–05.12", 400_000, True, None)]
    assert got["parts"][0]["lessons"] == 9 and sum(p["amount"] for p in got["parts"]) == got["amount"] == 800_000
    assert got["lessons"] == sum(p["lessons"] for p in got["parts"])


def test_v0_the_first_lesson_today_loses_nothing_so_the_period_is_whole():
    got = charge([bl.Stay(D(2026, 11, 3))], D(2026, 11, 3))                              # Tuesday 03.11 is the first lesson of November: 8 of 8
    assert got["parts"] == [{"label": "ноябрь", "period_start": "2026-11-01", "from": None, "amount": 400_000, "lessons": 8, "lessons_in_period": 8, "whole": True}]
    assert got["amount"] == 400_000 and got["lessons"] == 8


def test_v0_a_package_has_no_immediate_charge():
    lessons_mode = bl.Regime(mode=bl.LESSONS, package_size=8)
    assert charge([bl.Stay(D(2026, 10, 27))], D(2026, 11, 2), regime=lessons_mode) is None


def test_v0_the_price_zero_has_no_immediate_charge():
    assert charge([bl.Stay(D(2026, 10, 27))], D(2026, 11, 2), price=0) is None


# ---- Money-2 stage 7 Н1: a part «before» is taken off ONCE; a lock when the parts do not add up
def _part(start, amount, lessons=4):
    return {"label": "ноябрь", "period_start": start, "from": None, "amount": amount, "lessons": lessons, "lessons_in_period": None, "whole": True}


def test_n1_two_parts_of_one_period_after_the_return_take_the_part_before_off_once():
    before = {"amount": 100_000, "lessons": 4, "parts": [_part("2026-11-01", 100_000)]}
    after_parts = [_part("2026-11-01", 100_000), _part("2026-11-01", 60_000)]       # the closed stay first, then the new version of the rules
    amount, lessons, parts = bs._minus_before(160_000, 8, after_parts, before)
    assert amount == 60_000 and lessons == 4
    assert [p["amount"] for p in parts] == [60_000]                                  # not 60 000 - 100 000 = -40 000
    assert [(p["from"], p["whole"]) for p in parts] == [(None, True)]                # the second part had no part before: untouched (the reduced one dropped out with nothing more)


def test_n1_reduced_part_loses_from_and_whole_an_untouched_part_keeps_them():
    before = {"amount": 100_000, "lessons": 4, "parts": [_part("2026-11-01", 100_000)]}
    first = {**_part("2026-11-01", 160_000, 9), "from": "2026-11-17", "whole": False}      # reduced: was found
    second = {**_part("2026-12-01", 50_000, 3), "from": "2026-12-05", "whole": False}     # no part before: stays as it was
    amount, lessons, parts = bs._minus_before(210_000, 12, [first, second], before)
    assert (amount, lessons) == (110_000, 8)
    assert [(p["label"], p["from"], p["whole"], p["amount"]) for p in parts] == [("ноябрь", None, False, 60_000), ("ноябрь", "2026-12-05", False, 50_000)]
    whole_part = {**_part("2026-12-01", 50_000, 3), "whole": True}
    _, _, parts = bs._minus_before(180_000, 11, [{**_part("2026-11-01", 130_000, 8), "from": "2026-11-17", "whole": True}, whole_part], before)
    assert [(p["from"], p["whole"], p["amount"]) for p in parts] == [(None, False, 30_000), (None, True, 50_000)]      # was found -> not whole even if it was; no part before -> untouched


def test_n1_lock_parts_that_do_not_add_up_are_not_given_but_the_sum_stays():
    before = {"amount": 100_000, "lessons": 4, "parts": [_part("2026-11-01", 100_000)]}
    amount, lessons, parts = bs._minus_before(160_000, 8, [_part("2026-11-01", 100_000), _part("2026-12-01", 50_000)], before)   # the parts give 50 000, the sum says 60 000
    assert (amount, parts) == (60_000, [])
    amount, lessons, parts = bs._minus_before(160_000, 8, [_part("2026-10-01", 40_000), _part("2026-11-01", 100_000)], before)         # the sum says 60 000, the parts left give 40 000 - no breakdown
    assert (amount, parts) == (60_000, [])


def test_n1_lock_a_part_of_nothing_or_less_is_not_given():
    before = {"amount": 100_000, "lessons": 4, "parts": [_part("2026-11-01", 100_000)]}
    _, _, parts = bs._minus_before(50_000, 8, [_part("2026-11-01", 50_000)], before)            # 50 000 - 100 000 < 0
    assert parts == []


def test_n1_the_example_through_the_ledger_two_versions_of_the_rules_in_one_month():
    regs = [bl.Regime(anchor="fixed_day", billing_day=1), bl.Regime(effective_from=D(2026, 11, 15), anchor="fixed_day", billing_day=1)]
    kw = dict(price_at=flat(100_000, D(2026, 1, 1)), start=D(2026, 9, 1), regimes=regs, lesson_days=TUE_FRI)
    closed = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 10, 1), D(2026, 11, 10))], **kw)
    back = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 10, 1), D(2026, 11, 10)), bl.Stay(D(2026, 11, 20))], **kw)
    before = bs.immediate_from_ledger(bl.run_ledger(closed), closed.lesson_days)
    got = bs.immediate_from_ledger(bl.run_ledger(back), back.lesson_days, before)
    assert got is not None and got["amount"] == 38_000                               # the second November cell
    assert [p["amount"] for p in got["parts"]] == [38_000] and sum(p["amount"] for p in got["parts"]) == got["amount"]
