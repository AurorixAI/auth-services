"""Money-1 pack V0 (pure ledger, no database): «Сразу начислится» is told by the periods it is made of.

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
        {"label": "октябрь", "from": "2026-10-27", "amount": 89_000, "lessons": 2, "lessons_in_period": 9, "whole": False},
        {"label": "ноябрь", "from": None, "amount": 400_000, "lessons": 8, "lessons_in_period": None, "whole": True}]
    assert got["amount"] == 489_000 and got["lessons"] == 10                              # the old fields mean what they meant
    assert sum(p["amount"] for p in got["parts"]) == got["amount"] and sum(p["lessons"] for p in got["parts"]) == got["lessons"]
    assert got["text"] == "Сразу начислится 489 000 сум (за 10 уроков)"


def test_v0_the_payment_day_the_6th_names_the_period_by_its_days_not_by_a_month():
    got = charge([bl.Stay(D(2026, 10, 6))], D(2026, 11, 10), regime=bl.Regime())          # «свой месяц»: 06.10-05.11 and 06.11-05.12 have begun
    assert [(p["label"], p["amount"], p["whole"], p["from"]) for p in got["parts"]] == [
        ("06.10–05.11", 400_000, True, None), ("06.11–05.12", 400_000, True, None)]
    assert got["parts"][0]["lessons"] == 9 and sum(p["amount"] for p in got["parts"]) == got["amount"] == 800_000
    assert got["lessons"] == sum(p["lessons"] for p in got["parts"])


def test_v0_the_first_lesson_today_loses_nothing_so_the_period_is_whole():
    got = charge([bl.Stay(D(2026, 11, 3))], D(2026, 11, 3))                              # Tuesday 03.11 is the first lesson of November: 8 of 8
    assert got["parts"] == [{"label": "ноябрь", "from": None, "amount": 400_000, "lessons": 8, "lessons_in_period": 8, "whole": True}]
    assert got["amount"] == 400_000 and got["lessons"] == 8


def test_v0_a_package_has_no_immediate_charge():
    lessons_mode = bl.Regime(mode=bl.LESSONS, package_size=8)
    assert charge([bl.Stay(D(2026, 10, 27))], D(2026, 11, 2), regime=lessons_mode) is None


def test_v0_the_price_zero_has_no_immediate_charge():
    assert charge([bl.Stay(D(2026, 10, 27))], D(2026, 11, 2), price=0) is None
