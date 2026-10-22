#!/usr/bin/env python3
"""LOGIC-A: every cell of the scenario matrices of ``logicA_spec.md`` §2 has a test or a waiver.

Items (the tables of §2.1-§2.12): f1 f7 f9 f12 f13 f14 d1 (opening balance) d2 (pause) d3 (cancelled lesson) d4 (backdated price)
d5 (written-off days) d6 (payroll payouts).  Rows: n p o z r u c b m x d a.

A cell is covered when
  * a test function named ``test_logicA_<item>_<row>`` (or ``test_logicA_<item>_<row>_<words>``) exists under ``tests/``; or
  * ``MAPPED`` names an existing test that really covers it (``file::function`` - checked to exist); or
  * ``WAIVERS`` gives the reason it cannot be tested (a waiver without a reason fails).
Exit code 1 and the list of the open cells otherwise.   ``python scripts/check_matrix_coverage_a.py [-v]``
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
ITEMS = ["f1", "f7", "f9", "f12", "f13", "f14", "d1", "d2", "d3", "d4", "d5", "d6"]
ROWS = ["n", "p", "o", "z", "r", "u", "c", "b", "m", "x", "d", "a"]

PAYROLL = "tests/test_logicA_payroll_pg.py"
MONEY = "tests/test_logicA_money_pg.py"
OPEN = "tests/test_logicA_opening_pause_pg.py"
CONC = "tests/test_logicA_concurrency_pg.py"
LEDGER = "tests/test_billing_ledger_logicA.py"
GP = "tests/test_group_price_pg.py"

F1_N = f"{PAYROLL}::test_f1_g_f1_the_price_that_starts_mid_month_does_not_change_the_period_the_lesson_belongs_to"
F7_N = f"{PAYROLL}::test_f7_the_divisor_acts_from_a_month_and_old_months_do_not_move"
F12_N = f"{MONEY}::test_f12_the_running_period_is_not_cut_and_the_dry_run_says_who_switches"
F14_N = f"{PAYROLL}::test_f14_the_type_by_day_splits_the_line_and_the_fingerprint_is_mandatory"
LEAVE_N = f"{MONEY}::test_leave_preview_and_leave_with_a_refund"
LEFT_PAYS = f"{MONEY}::test_a_left_student_pays_the_debt_and_nothing_more"
F13_N = f"{MONEY}::test_f13_the_individual_price_ends_when_the_student_leaves_and_a_return_is_priced_by_the_group"
OPEN_RANGES = f"{OPEN}::test_opening_ranges_noop_replace_void_and_rights"
PAUSE_GOLD = f"{OPEN}::test_pause_golden_chain1_and_void_restores"
PAUSE_RANGES = f"{OPEN}::test_pause_dry_run_double_click_overlap_ranges"
CANCELLED = f"{MONEY}::test_a_cancelled_lesson_does_not_change_the_charge_of_an_incomplete_first_period"
PAYOUT_CHAIN = f"{PAYROLL}::test_g_pay_chain_pay_late_payment_to_pay_recalc_overpaid_void"
PAYOUT_MATRIX = f"{PAYROLL}::test_payout_matrix_errors_idempotency_rights_and_listing"
PRICE_REFUSED = f"{MONEY}::test_backdated_price_with_payments_is_refused_and_without_payments_it_shows_the_debt"
PRICE_FUTURE = f"{MONEY}::test_a_future_price_is_always_allowed_and_the_impact_counts_the_days_an_advance_buys"

MAPPED = {
    # F1
    "f1_n": F1_N, "f1_x": F1_N,
    "f1_p": f"{PAYROLL}::test_f1_unpaid_marks_keep_the_price_of_their_period_and_lessons_before_the_accounting_use_the_price_of_the_day",
    "f1_o": f"{LEDGER}::test_lesson_cover_prices_sources_and_before_start",
    "f1_b": f"{GP}::test_a_price_set_backdated_marks_the_saved_sheet_stale_and_the_sheet_itself_does_not_change",
    "f1_d": f"{PAYROLL}::test_a_lesson_in_a_backdated_pause_is_not_in_the_group_for_payroll",
    "f1_a": f"{GP}::test_a_price_raised_from_the_15th_splits_a_month_before_the_accounting_by_lesson_date",
    # F7
    "f7_n": F7_N, "f7_o": F7_N, "f7_r": F7_N, "f7_u": F7_N, "f7_b": F7_N, "f7_a": F7_N,
    "f7_x": "tests/test_payroll_pg.py::test_an_included_unpaid_lesson_of_a_package_student_is_priced_by_his_package",
    "f7_d": "tests/test_payroll_pg.py::test_the_divisor_is_a_centre_setting",
    # F9
    "f9_n": LEAVE_N, "f9_r": LEAVE_N, "f9_p": LEFT_PAYS, "f9_o": LEFT_PAYS,
    "f9_c": f"{MONEY}::test_leave_with_a_writeoff_and_the_balance_changed_answer",   # C: the numbers moved -> 409 BALANCE_CHANGED + fresh numbers
    "f9_d": f"{MONEY}::test_leave_rights_and_the_plain_delete",
    # F12
    "f12_n": F12_N, "f12_r": F12_N, "f12_b": F12_N, "f12_d": F12_N,
    "f12_x": f"{LEDGER.replace('billing_ledger_logicA', 'billing_ledger_logicA')}::test_defer_fixed_day_switches_on_the_next_cell_and_a_new_student_at_once",
    "f12_a": f"{LEDGER}::test_defer_fixed_day_switches_on_the_next_cell_and_a_new_student_at_once",
    # F13
    "f13_n": F13_N, "f13_p": F13_N,
    "f13_u": "tests/test_student_archive_pg.py::test_restore_returns_the_student_to_the_chosen_groups",
    "f13_b": "tests/test_postgres_migrations.py::test_empty_upgrade_repeat_and_checkpoint_upgrade_preserve_records",
    # F14
    "f14_n": F14_N, "f14_r": F14_N, "f14_c": F14_N, "f14_b": F14_N, "f14_m": F14_N,
    # D1
    "d1_n": f"{OPEN}::test_opening_a_paid_until_golden",
    "d1_p": f"{OPEN}::test_opening_b_debt_and_the_payment_that_closes_it_first",
    "d1_o": f"{LEDGER}::test_open_overpaying_goes_to_the_current_period_then_the_advance_and_refundable_has_no_free_days",
    "d1_z": OPEN_RANGES, "d1_r": OPEN_RANGES, "d1_u": OPEN_RANGES, "d1_b": OPEN_RANGES, "d1_d": OPEN_RANGES,
    "d1_c": f"{CONC}::test_an_opening_balance_and_a_payment_in_parallel",
    "d1_a": f"{MONEY}::test_import_creates_opening_balances_in_one_transaction_and_reports_what_was_skipped",
    # D2
    "d2_n": PAUSE_GOLD, "d2_u": PAUSE_GOLD, "d2_d": PAUSE_GOLD, "d2_r": PAUSE_RANGES, "d2_b": PAUSE_RANGES,
    "d2_z": f"{LEDGER}::test_pause_edges",
    "d2_c": f"{CONC}::test_a_pause_and_a_payment_in_parallel_both_land_and_the_account_reconciles",
    # D3
    "d3_p": CANCELLED, "d3_b": CANCELLED,
    "d3_x": "tests/test_payments_service_pg.py::test_cancelled_and_open_lessons_do_not_count",
    # D4
    "d4_n": PRICE_FUTURE, "d4_x": PRICE_FUTURE, "d4_p": PRICE_REFUSED, "d4_b": PRICE_REFUSED,
    "d4_r": f"{GP}::test_a_double_click_leaves_one_audit_row_and_changes_nothing",
    "d4_c": f"{CONC}::test_a_backdated_price_and_a_payment_in_parallel_never_rewrite_a_charged_period_with_money",
    # D5
    "d5_n": f"{PAYROLL}::test_decision5_written_off_days_are_paid_for_the_teacher_and_marked",
    "d5_o": "tests/test_pay2_api_pg.py::test_e6_writeoff_flow",
    # D6
    "d6_n": PAYOUT_CHAIN, "d6_u": PAYOUT_CHAIN, "d6_a": PAYOUT_CHAIN,
    "d6_p": PAYOUT_MATRIX, "d6_o": PAYOUT_MATRIX, "d6_r": PAYOUT_MATRIX, "d6_b": PAYOUT_MATRIX, "d6_m": PAYOUT_MATRIX,
    "d6_c": f"{CONC}::test_two_payouts_in_parallel_are_both_kept_and_a_payout_does_not_wait_for_a_recalculation",
}

WAIVERS = {
    "f9_b": "A departure «yesterday» is not supported by the spec (leaving = now; the past is a pause + a forgiven debt): no request field carries a date.",
    "d3_r": "Repeated cancellation of one lesson is a rule of the attendance module (returns True, writes nothing) that this pack does not touch.",
    "d3_u": "Spec: bringing a cancelled lesson back is not supported (a lesson is moved by the schedule); the ledger derives everything, no function exists.",
    "d3_c": "Cancel vs start of a lesson is decided by the group lock of the attendance module (_assert_no_open_attendance_for_groups), unchanged by this pack.",
    "d3_m": "A cancellation changes no charge in any month (S-4): the invariant is the whole of tests d3_n / d3_o / the incomplete-period test; there is no boundary left to test.",
    "d3_d": "No schedule: lessons do not take part in the charge by days, so there is nothing a cancellation could change.",
    "d3_a": "A lesson before the accounting start is never charged (before_start): a cancellation of it cannot change anything (covered by the ledger test of before_start).",
    "d5_c": "A write-off takes the same family lock as a refund / void / payment (PaymentService._lock_family); the lock itself is covered by test_a_refund_and_a_void_of_its_payment_never_both_win.",
}


def collected_tests() -> dict:
    found = {}
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.setdefault(node.name, []).append(path.relative_to(ROOT).as_posix())
    return found


def main(argv) -> int:
    tests = collected_tests()
    problems, rows = [], []
    for item in ITEMS:
        for row in ROWS:
            cell = f"{item}_{row}"
            named = [n for n in tests if re.fullmatch(rf"test_logicA_{cell}(_.*)?", n)]
            if named:
                rows.append((cell, "test", named[0]))
                continue
            if cell in MAPPED:
                path, _, func = MAPPED[cell].partition("::")
                if path not in tests.get(func, []):
                    problems.append(f"{cell}: the mapped test {MAPPED[cell]} does not exist")
                else:
                    rows.append((cell, "mapped", MAPPED[cell]))
                continue
            if WAIVERS.get(cell):
                rows.append((cell, "waiver", WAIVERS[cell]))
                continue
            problems.append(f"{cell}: no test, no mapping, no waiver with a reason")
    for cell, reason in WAIVERS.items():
        if not reason and not any(re.fullmatch(rf"test_logicA_{cell}(_.*)?", n) for n in tests):
            problems.append(f"{cell}: a waiver needs a reason")
    if "-v" in argv:
        for cell, kind, what in rows:
            print(f"{cell:8} {kind:7} {what}")
    counts = {k: sum(1 for _, kind, _ in rows if kind == k) for k in ("test", "mapped", "waiver")}
    print(f"matrix A: {len(ITEMS) * len(ROWS)} cells - {counts['test']} own tests, {counts['mapped']} mapped to existing tests, {counts['waiver']} waivers")
    for line in problems:
        print("OPEN:", line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
