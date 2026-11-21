"""PAY-TAILS С11 / С13 / С12 on the pure ledger (numbers by hand in the comments)."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import D, credit, flat, make


def entered(cid, on, amount):
    return credit(cid, on, amount, bl.WRITEOFF)


def test_c11_a_package_with_a_forgiveness_that_the_certificate_made_unneeded():
    # package 500 000 / 10 lessons (50 000 a lesson); three lessons held, 150 000 forgiven; then a certificate («по справке») makes one of them not counted: two counted = 100 000.
    # The forgiveness uses 100 000; the other 50 000 are thrown away - they are NOT a paid lesson: covered 2, left 0, advance 0
    reg = bl.Regime(mode=bl.LESSONS, package_size=10)
    held = [D(2026, 11, 3), D(2026, 11, 5), D(2026, 11, 6)]
    kw = dict(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(500_000), regimes=[reg], start=D(2026, 10, 1),
              credits=[entered("w", D(2026, 11, 7), 150_000)])
    three = bl.run_ledger(make(counted=[bl.CountedLesson(d, f"l{i}") for i, d in enumerate(held)], **kw))
    assert (three.charged, three.debt, three.advance, three.written_off) == (150_000, 0, 0, 150_000) and three.lessons.covered == 3 and three.lessons.left == 0
    two = bl.run_ledger(make(counted=[bl.CountedLesson(d, f"l{i}") for i, d in enumerate(held[:2])], **kw))
    assert (two.charged, two.debt, two.advance, two.refundable, two.written_off, two.written_off_granted) == (100_000, 0, 0, 0, 100_000, 150_000)
    assert (two.lessons.covered, two.lessons.used, two.lessons.left) == (2, 2, 0)           # not 3 covered / 1 left


def test_c13_an_opening_debt_forgiven_then_corrected_down_the_rest_lies_on_the_oldest_open_period():
    # opening debt 100 000 forgiven (entered 05.11), then corrected to 60 000: 60 000 close the opening, the other 40 000 lie on November (it had begun before) - «forgave 100 000 of the oldest debt»
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(420_000), start=D(2026, 10, 1), credits=[entered("w", D(2026, 11, 5), 100_000)])
    inp = replace(inp, opening=bl.Opening(debt_amount=60_000, debt_on=D(2026, 10, 1)))
    L = bl.run_ledger(inp)
    assert (L.charged, L.written_off, L.written_off_granted, L.debt, L.advance) == (480_000, 100_000, 100_000, 380_000, 0) and L.reconciles
