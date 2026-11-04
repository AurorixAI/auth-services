"""Money-1 A1b: a payout is by the SAVED total of the sheet; what changed after the save is a line of its own (ЗП-04, ЗП-05, ЗПД-1 of ``area-payroll-devil.md``).  Pure: no database."""
from src.auth.payroll_payouts import state_of
from src.auth.payroll_schemas import PayoutOut


def payout(amount):
    return PayoutOut(id="1", sheet_id="1", teacher_id="1", month="2026-10", amount=amount, paid_on="2026-11-02", method="cash", note=None, sheet_revision=1, sheet_total=0,
                     created_by_name=None, created_at="2026-11-02T00:00:00", voided_at=None, void_reason=None)


def test_zp04_saved_1_000_000_fresh_800_000_paid_1_000_000_is_settled_and_says_minus_200_000():
    """Before: «overpaid», balance −200 000, basis «fresh», the Excel «Заработано» 800 000 against rows that sum to 1 000 000."""
    state = state_of(saved_total=1_000_000, fresh_total=800_000, items=[payout(1_000_000)])
    assert (state.state, state.balance, state.target, state.basis, state.advance_out) == ("settled", 0, 1_000_000, "saved", 0)
    assert state.changed_by == -200_000 and "−200 000" in state.change_text and "Пересохраните" in state.change_text
    assert state.text == "Выплачено полностью"


def test_zpd1_mirror_saved_800_000_fresh_1_000_000_the_owner_is_not_let_to_pay_the_fresh_sum_silently():
    """Before: target 1 000 000 (fresh), paid 1 000 000 -> settled with advance_out 0, but ``carry_ins`` (saved 800 000) handed 200 000 to November: the phantom advance.
    Now the target is 800 000 on both sides: paying 1 000 000 is an overpay of 200 000 (the confirmation of the advance), and the chain agrees with the page."""
    due = state_of(saved_total=800_000, fresh_total=1_000_000, items=[])
    assert (due.state, due.target, due.balance, due.changed_by) == ("unpaid", 800_000, 800_000, 200_000)
    paid_all = state_of(saved_total=800_000, fresh_total=1_000_000, items=[payout(1_000_000)])
    assert (paid_all.state, paid_all.balance, paid_all.advance_out) == ("overpaid", -200_000, 200_000)      # the same 200 000 that ``carry_ins`` carries to November


def test_a_sheet_that_did_not_change_has_no_line_about_a_change():
    state = state_of(saved_total=500_000, fresh_total=500_000, items=[payout(200_000)])
    assert (state.changed_by, state.change_text, state.state, state.balance) == (0, None, "due", 300_000)
    assert state_of(saved_total=500_000, fresh_total=None, items=[]).changed_by == 0                # no rate / a manual price: nothing to compare
