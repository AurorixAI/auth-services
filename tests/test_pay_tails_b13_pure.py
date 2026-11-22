"""PAY-TAILS Б11.18 п. 4, step 8.13.4: the journal of the cancelled leavings is asked ONLY when a piece of a stay falls out AND passes the checks of the dates (a piece that does not touch the leaving day carries nothing, so the
journal is not read for it)."""
from datetime import date, datetime
from types import SimpleNamespace


from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls

D = date


def periods(second_joined):
    # the first piece 01.11 - 20.11 (the leaving at 20.11 09:00 local = 04:00 UTC); the second piece opened on ``second_joined`` and closed the SAME day without a mark: a piece of no day
    first = SimpleNamespace(joined_at=datetime(2026, 11, 1, 3, 0), left_at=datetime(2026, 11, 20, 4, 0), billing_from=None)
    second = SimpleNamespace(joined_at=datetime(2026, 11, second_joined, 5, 0), left_at=datetime(2026, 11, second_joined, 6, 0), billing_from=None)
    return [first, second]


def test_the_journal_is_not_read_for_a_piece_of_no_day_that_does_not_touch_the_leaving_day():
    def forbidden(membership_id):
        raise AssertionError("the journal of the cancelled leavings was asked for a piece that cannot carry the moment")

    stays = ls.stays_of_membership(SimpleNamespace(id=1, joined_at=None, is_active=False, left_at=None), periods(22), lambda day: False, undone_at=forbidden)
    assert [(s.first, s.last) for s in stays] == [(D(2026, 11, 1), D(2026, 11, 20))]


def test_the_journal_is_read_once_for_a_piece_of_no_day_opened_on_the_leaving_day():
    asked = []

    def journal(membership_id):
        asked.append(membership_id)
        return {datetime(2026, 11, 20, 4, 0)}

    stays = ls.stays_of_membership(SimpleNamespace(id=7, joined_at=None, is_active=False, left_at=None), periods(20), lambda day: False, undone_at=journal)
    assert asked == [7]
    assert [(s.first, s.last, s.left_at) for s in stays] == [(D(2026, 11, 1), D(2026, 11, 20), datetime(2026, 11, 20, 6, 0))]       # the cancelled leaving: the moment of the later one
    assert bl.NO_MOMENT > stays[0].left_at
