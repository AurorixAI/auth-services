"""PAY-TAILS Б11.35 п. 5, step 8.14.8: М3 (a piece of a payment that points at no period of the account is said in the log of errors, the line stays «Аванс») and М6 (the sign «a leaving took the pause off whole» is the
journal, never the words of the reason: lesson 165).  Pure: no database."""
import logging
from datetime import date, datetime
from types import SimpleNamespace

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.auth import payments_views as pv

D = date


def piece(segment_id, amount):
    return bl.Allocation("pay", segment_id, amount, 1, bl.ADVANCE if segment_id == bl.ADVANCE_SEGMENT else "current", True, None, "x", D(2026, 11, 1), D(2026, 11, 30), 0)


def test_m3_a_piece_that_points_at_no_period_is_logged_as_an_error_and_shown_as_the_advance_the_sum_of_the_lines_is_the_payment(caplog):
    ledger = SimpleNamespace(segments=[], allocations=[piece("gone", 300_000), piece(bl.ADVANCE_SEGMENT, 100_000)], mode=bl.CALENDAR)
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        rows = pv.money_rows(ledger, "pay", D(2026, 12, 10))
    assert [(r.label, r.amount, r.state) for r in rows] == [("Аванс", 400_000, "advance")]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "gone" in errors[0].getMessage() and "300000" in errors[0].getMessage().replace(" ", ""), [r.getMessage() for r in errors]


def test_m3_the_advance_itself_is_no_error(caplog):
    ledger = SimpleNamespace(segments=[], allocations=[piece(bl.ADVANCE_SEGMENT, 100_000)], mode=bl.CALENDAR)
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        rows = pv.money_rows(ledger, "pay", D(2026, 12, 10))
    assert [(r.label, r.amount) for r in rows] == [("Аванс", 100_000)] and not [r for r in caplog.records if r.levelno == logging.ERROR]


def loader_with(lines):
    loader = ls.LedgerLoader.__new__(ls.LedgerLoader)
    loader.pause_lines = lines
    return loader


def row(voided_at, reason, first=17, last=25):
    return SimpleNamespace(id="p1", membership_id="m1", created_at=datetime(2026, 11, 9, 3), first_day=D(2026, 11, first), last_day=D(2026, 11, last), voided_at=voided_at, void_reason=reason)


def test_m6_the_words_of_the_reason_do_not_say_a_leaving_took_the_pause_off_the_journal_does():
    moment = datetime(2026, 11, 19, 3)
    entering = (datetime(2026, 11, 9, 3), ls.AUDIT_PAUSE_SET, {"first_day": "2026-11-17", "last_day": "2026-11-30"})
    # a row the journal does not end in (17 - 25 against 17 - 30) with a taken-off mark that has the words of a leaving, but the journal has NO cut: it is a pause a human took off - not a pause a leaving took off whole
    unsound = loader_with({"p1": [entering]})._pause_of(row(moment, ls.LEAVE_VOID_REASON))
    assert unsound.voided_at is None
    # the same row, the journal says a leaving took it off whole (a cut with no days left): the moment is the one of the row, whatever the words of the reason
    cut = (moment, ls.AUDIT_PAUSE_VOID, {"first_day": "2026-11-17", "last_day": "2026-11-25", "was": ["2026-11-17", "2026-11-30"], "via": "leave"})
    off = loader_with({"p1": [entering, cut]})._pause_of(row(moment, "Передумали", 17, 25))
    assert off.voided_at == moment
