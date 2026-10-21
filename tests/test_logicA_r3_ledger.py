"""LOGIC-A, fixes of the lead's R3 review on the pure ledger (m2, ...)."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import credit, flat

D = date


def _inp(**kw):
    base = dict(today=D(2026, 10, 21), accounting_start=D(2026, 9, 1), stays=(bl.Stay(D(2026, 10, 1)),), regimes=(bl.Regime(),), price_at=flat(0))
    base.update(kw)
    return bl.LedgerInput(**base)


def test_m2_a_student_with_the_price_zero_stays_free_with_a_pause_or_an_opening():
    assert bl.run_ledger(_inp()).status.status == ps.FREE
    assert bl.run_ledger(_inp(pauses=(bl.Pause(D(2026, 10, 5), D(2026, 10, 9)),))).status.status == ps.FREE
    assert bl.run_ledger(_inp(pauses=(bl.Pause(D(2026, 10, 18), D(2026, 10, 25)),))).status.status == ps.FREE        # even while the pause lasts
    assert bl.run_ledger(_inp(opening=bl.Opening(paid_until=D(2026, 10, 15)))).status.status == ps.FREE
    assert bl.run_ledger(_inp(opening=bl.Opening(debt_amount=50_000, debt_on=D(2026, 9, 30)))).status.status == ps.FREE
    paid = bl.run_ledger(_inp(credits=(credit("p", D(2026, 10, 1), 100_000),)))                                       # real money: as before
    assert paid.status.status != ps.FREE
