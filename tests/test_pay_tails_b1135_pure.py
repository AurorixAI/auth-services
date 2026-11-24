"""PAY-TAILS Б11.35 п. 1 + Б11.36 п. 2, step 8.14.8: the worlds of the pauses of a participation (``billing_ledger._pause_worlds``) by the table of the 11 cases.  Pure: no database.
November 2026, the pause A = 17 - 30.11 entered 09.11.  A day ``d`` is a day of a pause in the world of ``T`` iff the LAST event about ``d`` written not after ``T`` (a form of a human that has the day / a cut that took it
out) is a form of a human AND (a) ``d`` is a day of a LIVE pause today, or (b) a cut that stands (on a pause no human took off) took the day out LATER than ``T``.  A pause the core reads as known always (no forms: the
journal does not end in its days) gives its days of today to EVERY world."""
from datetime import date, datetime, timedelta

from src.auth import billing_ledger as bl

D = date


def N(day):
    return D(2026, 11, day)


def at(day, hour=3, minute=0):
    return datetime(2026, 11, day, hour, minute)


def days(pauses):
    return sorted(d.day for p in pauses for d in (p.first + timedelta(days=k) for k in range((p.last - p.first).days + 1)))


def span(a, b):
    return list(range(a, b + 1))


A_FORM = ((at(9), N(17), N(30)),)
CUT_A = ((at(19), N(20), N(30)),)                                                                    # the leaving of 19.11 took 20 - 30 out of A


def a_cut():
    """A after the leaving of 19.11: the row 17 - 19, the cut stands."""
    return bl.Pause(N(17), N(19), A_FORM, None, CUT_A)


def b_form(first=24, last=30, at_day=22):
    return ((at(at_day, 5), N(first), N(last)),)


def has25(world, *moments):
    return [25 in days(world(m)) for m in moments]


# 1, 2: the leaving 19.11 - before it the day is a pause, from it not
def test_row1_row2_the_leaving_hides_the_tail_only_from_the_worlds_from_its_moment():
    world = bl._pause_worlds([a_cut()], [])
    assert has25(world, at(10), at(19, 2, 59)) == [True, True]
    assert has25(world, at(19), at(19, 4), at(25), bl.PROBE_MOMENT) == [False, False, False, False]


# 3: B 24 - 30 entered 22.11 after the return: its days are a pause from 22.11
def test_row3_a_new_pause_after_the_return_brings_the_days_of_the_old_tail_back():
    b = bl.Pause(N(24), N(30), b_form())
    world = bl._pause_worlds([a_cut(), b], [])
    assert has25(world, at(21), at(22, 4), at(22, 6), bl.PROBE_MOMENT) == [False, False, True, True]
    assert days(world(bl.PROBE_MOMENT)) == span(17, 19) + span(24, 30)


# 4: B taken off by the human on 23.11: no world from 22.11 has the day 25 (it is not a day of a live pause, no cut after)
def test_row4_the_new_pause_taken_off_by_the_human_leaves_no_ghost_of_the_old_tail():
    b_off = bl.Pause(N(24), N(30), b_form())
    world = bl._pause_worlds([a_cut()], [b_off])
    assert has25(world, at(22, 6), at(23, 4), at(23, 12), bl.PROBE_MOMENT) == [False, False, False, False]
    assert days(world(bl.PROBE_MOMENT)) == span(17, 19)


# 5: B shortened to 24 - 24: only the day 24 is a pause
def test_row5_the_new_pause_shortened_by_the_human_keeps_only_its_days_of_today():
    b = bl.Pause(N(24), N(24), ((at(22, 5), N(24), N(30)), (at(23, 5), N(24), N(24))))
    world = bl._pause_worlds([a_cut(), b], [])
    assert has25(world, at(22, 6), at(23, 4), at(23, 12), bl.PROBE_MOMENT) == [False, False, False, False]
    assert days(world(at(22, 6))) == span(17, 19) + [24] and days(world(bl.PROBE_MOMENT)) == span(17, 19) + [24]


# 6: the same as 4, before the leaving: the world before the leaving does not change
def test_row6_a_world_before_the_leaving_is_not_changed_by_what_came_after():
    b_off = bl.Pause(N(24), N(30), b_form())
    world = bl._pause_worlds([a_cut()], [b_off])
    assert days(world(at(10))) == span(17, 30) and days(world(at(19, 2))) == span(17, 30)


# 7: 23.11 the SECOND leaving took the future B off whole (it stands)
def test_row7_the_second_leaving_took_the_new_pause_off_whole_the_world_between_keeps_its_days():
    b_gone = bl.Pause(N(24), N(30), b_form(), at(23, 5))
    world = bl._pause_worlds([a_cut()], [b_gone])
    assert has25(world, at(22, 6), at(23, 4), at(23, 5), at(23, 12), bl.PROBE_MOMENT) == [True, True, False, False, False]
    assert days(world(at(22, 6))) == span(17, 19) + span(24, 30)


# 8: the leaving 19.11, the human took A off, an ordinary return: the pause is «as if it never was», with its tail
def test_row8_the_pause_a_human_took_off_after_the_cut_has_no_cut_in_any_world():
    a_off = bl.Pause(N(17), N(19), A_FORM, None, CUT_A)                                                 # in ``past`` with no ``voided_at``: a human took it off
    world = bl._pause_worlds([], [a_off])
    for moment in (at(9), at(10), at(18), at(19, 4), at(21), at(25), bl.PROBE_MOMENT):
        assert days(world(moment)) == [], moment


# 9: the leaving cancelled: A is back, 17 - 30, no cut
def test_row9_the_cancelled_leaving_gives_the_pause_back_whole():
    a = bl.Pause(N(17), N(30), A_FORM)
    world = bl._pause_worlds([a], [])
    assert days(world(at(8))) == [] and has25(world, at(9), at(19, 4), bl.PROBE_MOMENT) == [True, True, True]


# 10: the leaving 10:00 cut A, 10:05 the human took A off, 10:15 the leaving cancelled, 10:20 a new pause B 17 - 30: B is a pause in the worlds from 09.11 (the cut is no longer in the way, in ANY form of the journal)
def test_row10_the_pause_after_the_cancel_has_the_days_in_the_worlds_from_the_first_form_the_cut_on_a_taken_off_pause_does_not_hide_them():
    b = bl.Pause(N(17), N(30), ((at(10, 10, 20), N(17), N(30)),))
    gone_cancelled = bl.Pause(N(17), N(30), A_FORM)                                                     # the cancel wrote its line: no cut among the facts
    gone_standing = bl.Pause(N(17), N(19), A_FORM, None, ((at(10, 10, 0), N(20), N(30)),))          # no such line: the cut still stands in the journal
    for off in (gone_cancelled, gone_standing):
        world = bl._pause_worlds([b], [off])
        assert has25(world, at(9, 4), at(10, 10, 2), at(10, 10, 12), at(10, 11), bl.PROBE_MOMENT) == [True] * 5, off


# 11: no leavings: shorten and bring back (the shortening of the days alone is no event about a day: the days come back as they were) / take off and put again / split
def test_row11_forms_of_humans_alone_the_days_are_a_pause_from_the_first_form_that_has_them():
    shorten_back = bl.Pause(N(17), N(30), ((at(9), N(17), N(30)), (at(10), N(17), N(25)), (at(11), N(17), N(30))))
    world = bl._pause_worlds([shorten_back], [])
    assert days(world(at(9, 4))) == span(17, 30) and days(world(at(10, 4))) == span(17, 30) and days(world(at(11, 4))) == span(17, 30)
    off = bl.Pause(N(17), N(30), A_FORM)
    again = bl.Pause(N(17), N(30), ((at(12), N(17), N(30)),))
    world = bl._pause_worlds([again], [off])
    assert days(world(at(9, 4))) == span(17, 30) and days(world(at(13))) == span(17, 30)
    first, second = bl.Pause(N(17), N(20), ((at(12), N(17), N(30)), (at(12, 5), N(17), N(20)))), bl.Pause(N(24), N(30), ((at(12, 5), N(24), N(30)),))
    world = bl._pause_worlds([first, second], [])
    assert days(world(at(13))) == span(17, 20) + span(24, 30) and days(world(at(12, 3, 1))) == span(17, 20) + span(24, 30)


# Б11.36 п. 2: the pause the core reads as known always gives its days of today to every world; a cut of another pause does not hide them
def test_b11_36_2_a_pause_read_as_known_always_is_in_every_world_a_cut_of_another_pause_does_not_hide_its_days():
    b_unsound = bl.Pause(N(24), N(30))                                                                  # no forms: the journal does not end in its days
    world = bl._pause_worlds([a_cut(), b_unsound], [])
    assert days(world(bl.PROBE_MOMENT)) == span(17, 19) + span(24, 30)
    assert days(world(at(20))) == span(17, 19) + span(24, 30) and days(world(at(10))) == span(17, 30) and days(world(datetime.min)) == span(24, 30)
    # the days of the live pauses today are the world «now» (the strict property of the generator)
    assert days(world(bl.PROBE_MOMENT)) == days([a_cut(), b_unsound])


# Б11.36 п. 9: a form and a cut at ONE moment (one transaction): the cut is after the form.  Not reachable by the routes: the form of a pause (the entering, an edit) and a cut of a leaving are written by different requests, every line
# is stamped by its own reading of the clock (``logica_service._audit`` / ``close_at_leave`` run in the transaction of the leaving, which does not write a form of the pause it cuts); the order is the key ``(moment, kind)`` of ``_pause_worlds``.
def test_b11_36_9_a_form_and_a_cut_of_one_moment_the_cut_is_after_the_form():
    p = bl.Pause(N(17), N(19), ((at(9), N(17), N(30)), (at(19), N(17), N(30))), None, ((at(19), N(20), N(30)),))
    world = bl._pause_worlds([p], [])
    assert days(world(at(19))) == span(17, 19) and days(world(at(19, 2, 59))) == span(17, 30) and days(world(bl.PROBE_MOMENT)) == span(17, 19)
