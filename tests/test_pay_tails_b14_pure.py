"""PAY-TAILS Б11.30, step 8.14: the ONE reader of the journal of a pause (``ledger_service.read_pause_journal``) and the worlds of the pauses of a participation by the DAYS (``billing_ledger._pause_worlds``).  Pure: no database.
Every expectation is a set of days worked out by the two conditions of Б11.30 п. 1: a day is a day of a pause in the world of ``T`` iff it is in the TOTAL (the days of the live pauses as they are plus the days a standing
cut hides) and the LAST event about it written not after ``T`` (a form of a human that has it / a cut that removed it) is a form of a human."""
from datetime import date, datetime, timedelta

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls

D = date
SET, VOID = ls.AUDIT_PAUSE_SET, ls.AUDIT_PAUSE_VOID


def N(day):
    return D(2026, 11, day)


def at(day, minute=0):
    return datetime(2026, 11, day, 3, 0) + timedelta(minutes=minute)


def snap(a, b, **more):
    return {"first_day": N(a).isoformat(), "last_day": N(b).isoformat(), **more}


def was(a, b):
    return [N(a).isoformat(), N(b).isoformat()]


def read(lines, row, *, voided=False, created=at(9)):
    return ls.read_pause_journal(lines, created_at=created, first_day=N(row[0]), last_day=N(row[1]), voided=voided)


# ---------------------------------------------------------------------------------------------------------------------------- the reader of the journal
def test_the_entering_and_the_edits_of_a_human_are_the_forms_no_cut_stands():
    j = read([(at(9), SET, snap(17, 30)), (at(12), SET, snap(13, 30, was=was(17, 30)))], (13, 30))
    assert j.forms == ((at(9), N(17), N(30)), (at(12), N(13), N(30))) and j.cut is None and j.sound


def test_a_cut_of_a_leaving_is_not_a_form_it_has_the_days_it_took_out():
    j = read([(at(9), SET, snap(17, 30)), (at(19), SET, snap(17, 19, was=was(17, 30), via="leave"))], (17, 19))
    assert j.forms == ((at(9), N(17), N(30)),) and j.sound and not j.leave_voided
    assert j.cut == ls.PauseCut(at(19), (N(17), N(30)), (N(17), N(19))) and j.cut.removed == (N(20), N(30))


def test_the_cancel_of_the_leaving_erases_the_cut():
    lines = [(at(9), SET, snap(17, 30)), (at(19), SET, snap(17, 19, was=was(17, 30), via="leave")), (at(19, 5), SET, snap(17, 30, was=was(17, 19), via="leave_undone"))]
    j = read(lines, (17, 30))
    assert j.cut is None and j.forms == ((at(9), N(17), N(30)),) and j.sound


def test_a_pause_the_leaving_took_off_whole_is_a_cut_of_all_its_days_and_the_cancel_with_no_was_gives_it_back():
    lines = [(at(9), SET, snap(24, 30)), (at(19), VOID, snap(24, 30, was=was(24, 30), via="leave"))]
    j = read(lines, (24, 30), voided=True)
    assert j.leave_voided and j.cut.removed == (N(24), N(30)) and j.sound
    back = read([*lines, (at(19, 5), SET, snap(24, 30, was=None, via="leave_undone"))], (24, 30))
    assert back.cut is None and back.sound


def test_the_entering_of_a_pause_a_transfer_carried_is_a_form_of_a_human_not_a_cut():
    j = read([(at(19), SET, snap(20, 30, moved_from_pause_id="x", via="transfer"))], (20, 30), created=at(19))
    assert j.forms == ((at(19), N(20), N(30)),) and j.cut is None and j.sound


def test_a_human_who_takes_the_pause_off_after_the_cut_leaves_the_cut_among_the_facts():
    lines = [(at(9), SET, snap(17, 30)), (at(19), SET, snap(17, 19, was=was(17, 30), via="leave")), (at(20), VOID, snap(17, 19, reason="Передумали"))]
    j = read(lines, (17, 19), voided=True)
    assert j.cut is not None and not j.leave_voided and j.sound


def test_a_row_the_journal_does_not_end_in_is_not_sound_and_a_pause_without_the_entering_line_is_read_from_the_first_edit():
    assert not read([(at(9), SET, snap(17, 30))], (17, 25)).sound                              # a change nobody wrote
    j = read([(at(12), SET, snap(13, 30, was=was(17, 30)))], (13, 30))
    assert j.forms == ((at(9), N(17), N(30)), (at(12), N(13), N(30))) and j.sound            # the first edit says what the pause was
    assert read([], (17, 30)).forms == ((at(9), N(17), N(30)),)


# ---------------------------------------------------------------------------------------------------------------------------- the worlds of the pauses by the days
def days(pauses):
    return sorted(d.day for p in pauses for d in (p.first + timedelta(days=k) for k in range((p.last - p.first).days + 1)))


def span(a, b):
    return list(range(a, b + 1))


def test_w3_the_old_pause_is_cut_by_the_leaving_a_new_pause_gets_the_days_the_old_one_had_in_the_worlds_before_the_leaving():
    old = bl.Pause(N(17), N(19), ((at(9), N(17), N(30)),), None, ((at(19, 5), N(20), N(30)),))
    new = bl.Pause(N(24), N(30), ((at(22, 30), N(24), N(30)),), at(23, 5))                      # taken off by the second leaving
    world = bl._pause_worlds([old], [new])
    assert days(world(at(10))) == span(17, 30)                                                  # the old pause as entered
    assert days(world(at(19, 10))) == span(17, 19)                                              # the cut: the days after the leaving are out
    assert days(world(at(22, 40))) == span(17, 19) + span(24, 30)                               # the new pause: its days were a pause in the older worlds, the form of a human brings them back
    assert days(world(at(23, 10))) == span(17, 19)                                              # the second leaving took the new pause off
    assert days(bl._pauses_known_at([old], [new], bl.PROBE_MOMENT)) == span(17, 19)


def test_d3_a_pause_a_human_took_off_still_gives_its_days_to_the_new_pause_on_the_same_days():
    off = bl.Pause(N(17), N(30), ((at(9), N(17), N(30)),))                                      # taken off by a human: in ``past`` with no moment of a leaving
    new = bl.Pause(N(17), N(30), ((at(13), N(17), N(30)),))
    world = bl._pause_worlds([new], [off])
    assert days(world(at(10))) == span(17, 30) and days(world(at(13))) == span(17, 30)
    assert days(bl._pause_worlds([], [off])(at(10))) == []                                      # a pause taken off and nothing else: it was never a pause (not in the total)


def test_a_day_a_human_took_off_is_in_no_world_and_a_day_he_added_is_known_from_the_form_that_has_it():
    p = bl.Pause(N(13), N(25), ((at(9), N(17), N(30)), (at(12), N(13), N(25))))
    world = bl._pause_worlds([p], [])
    assert days(world(at(10))) == span(17, 25)                                                  # 26 - 30 were taken off: no world; 13 - 16 are added at 12.11: not yet
    assert days(world(at(12))) == span(13, 25)


def test_a_pause_without_a_stamp_is_known_always_and_no_events_no_pauses():
    assert days(bl._pause_worlds([bl.Pause(N(17), N(20))], [])(datetime.min)) == span(17, 20)
    assert bl._pause_worlds([], [])(at(10)) == ()
