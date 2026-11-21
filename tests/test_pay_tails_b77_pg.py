"""PAY-TAILS Б7.7 step 3.4 / 3.6: the form of a pause in TIME comes from the journal of decisions (the line of ``membership_pause_set``: the moment and the days of every entering and every edit).
PostgreSQL, through the routes of the product; every number is worked out by hand in the comment.

World (ОМ, as in ``test_pay_tails_money_pg``): 600 000 a month, Tue / Thu / Fri, billing day 1, the student joined 02.11.  November has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each.
On 03.11 a pause 24.11 - 30.11 is entered (three lessons lost: 9 lessons, 450 000).  The pause is EDITED on 12.11 to 17.11 - 30.11 (six lessons lost: 300 000)."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_money_pg import check, forgive, led, om

pytestmark = needs_pg
D = date


def pause_to_edit(w, fixed_today):
    g, s = om(w, fixed_today)                                                           # the clock stands on 03.11
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/pauses", json={"first_day": "2026-11-24", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    return g, s, r.json()["pause"]["id"]


def extend(w, pause_id):
    r = owner(w).patch(f"{API}/org/pauses/{pause_id}", json={"first_day": "2026-11-17", "reason": "Болезнь продлилась"})
    assert r.status_code == 200, r.text


def november(w, s, g, today):
    (nov,) = [x for x in led(w, s, g, today).segments if x.kind == "period"][:1]
    return nov


def test_chain_1_the_pause_is_extended_after_half_the_rest_was_forgiven_and_the_parent_paid_the_other_half(world, fixed_today):
    # 05.11: November costs 450 000 -> 225 000 are forgiven (half).  10.11: the parent pays the other 225 000.  12.11: the pause is EXTENDED (November 300 000).  The extension is entered AFTER both:
    # the forgiveness loses first - 450 000 - 300 000 = 150 000 are cut off it (alive 75 000); the money stays where it is.  November: money 225 000 + forgiveness 75 000 = 300 000.
    # Debt 0, advance 0, used 75 000 of 225 000.  (A pause that looked «known since the day it was entered» with its LAST days gave an advance of 150 000 made of the forgiveness.)
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 225_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=225_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    extend(world, pause_id)
    fixed_today(D(2026, 11, 15))
    check(world, s, g, D(2026, 11, 15), charged=300_000, debt=0, advance=0, used=75_000, granted=225_000)
    nov = november(world, s, g, D(2026, 11, 15))
    assert (nov.amount, nov.money, nov.forgiven) == (300_000, 225_000, 75_000)


def test_chain_2_the_pause_is_extended_after_the_rest_was_forgiven_and_the_next_month_was_paid(world, fixed_today):
    # 05.11: the whole rest of November 450 000 is forgiven.  10.11: the parent pays DECEMBER (600 000).  12.11: the pause is extended (November 300 000): 150 000 are cut off the forgiveness (alive 300 000),
    # December keeps its money.  November: forgiveness 300 000; debt 0; advance 600 000 (money: December has not begun); used 300 000 of 450 000.  (Taking the moment of the EDIT as the moment of the
    # pause would have charged November 150 000 to the family for nothing; taking the moment of the entering gave the same numbers.)
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 450_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    extend(world, pause_id)
    fixed_today(D(2026, 11, 15))
    check(world, s, g, D(2026, 11, 15), charged=300_000, debt=0, advance=600_000, used=300_000, granted=450_000)
    nov = november(world, s, g, D(2026, 11, 15))
    assert (nov.amount, nov.money, nov.forgiven) == (300_000, 0, 300_000)


def test_every_path_that_changes_the_days_of_a_pause_writes_the_days_and_the_form_before(world, fixed_today):
    # The forms of a pause in time are read from the journal: the entering (the days), an edit (the days and ``was``), the cut of a leaving (the days and ``was``), the cancel of the leaving (the days and ``was`` =
    # the form it had until then).  A pause 22.11 - 24.11 and another 25.11 - 28.11; the leaving on 22.11 cuts the first to 22.11 - 22.11 and cancels the second; the return the same day gives both back;
    # then the first is edited to 20.11 - 24.11.  Forms of the first at the end: entered (22-24), cut (22-22), restored (22-24), edited (20-24); the loader gives the same days as the row.
    from src.auth import ledger_service as ls
    from src.models.auth import GroupAudit, MembershipPause
    from tests.test_money2_transfer_a_pg import add, groups3, joined, pause as make_pause, rich_stay  # noqa: F401

    a, b, c = groups3(world, fixed_today, D(2026, 11, 22))
    s0 = rich_stay(world, a)
    mid = membership(world, s0, a).id
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).status_code == 200
    add(world, s0, group=a, mode="add")                                                    # the return the same day = the cancel of the leaving
    world.db.expire_all()
    first = world.db.query(MembershipPause).filter_by(membership_id=mid, first_day=D(2026, 11, 22)).one()
    r = owner(world).patch(f"{API}/org/pauses/{first.id}", json={"first_day": "2026-11-20", "reason": "Болезнь началась раньше"})
    assert r.status_code == 200, r.text
    world.db.expire_all()
    lines = [(g.snapshot["first_day"], g.snapshot["last_day"], g.snapshot.get("was")) for g in world.db.query(GroupAudit).filter(
        GroupAudit.org_id == world.org.id, GroupAudit.action == ls.AUDIT_PAUSE_SET).order_by(GroupAudit.created_at, GroupAudit.id) if g.snapshot.get("pause_id") == str(first.id)]
    assert lines == [("2026-11-22", "2026-11-24", None), ("2026-11-22", "2026-11-22", ["2026-11-22", "2026-11-24"]),
                     ("2026-11-22", "2026-11-24", ["2026-11-22", "2026-11-22"]), ("2026-11-20", "2026-11-24", ["2026-11-22", "2026-11-24"])], lines
    loader = ls.LedgerLoader(world.db, str(world.org.id), D(2026, 11, 22))
    loader.load_facts([mid])
    (mine,) = [p for p in loader.build_input(mid).pauses if p.first == D(2026, 11, 20)]
    assert [(f, l) for _, f, l in mine.forms] == [(D(2026, 11, 22), D(2026, 11, 24)), (D(2026, 11, 22), D(2026, 11, 22)), (D(2026, 11, 22), D(2026, 11, 24)), (D(2026, 11, 20), D(2026, 11, 24))]
    assert all(x[0] <= y[0] for x, y in zip(mine.forms, mine.forms[1:])) and mine.forms[-1][1:] == (mine.first, mine.last)


# ---------------------------------------------------------------------------------------------------------------------------- step 3.6: the worlds of B7 on the routes (numbers by hand)
from tests.test_pay2_api_pg import preview                                    # noqa: E402
from tests.test_pay_tails_money_pg import lp                                  # noqa: E402


def pause_on(w, s, g, first, last, reason="Болезнь"):
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/pauses", json={"first_day": first, "last_day": last, "reason": reason})
    assert r.status_code == 201, r.text
    return r.json()["pause"]["id"]


def periods_of(w, s, g, today):
    return [x for x in led(w, s, g, today).segments if x.kind == "period"]


def test_b7_1_a_pause_entered_between_the_forgiveness_and_a_payment_dated_back_leaves_no_debt(world, fixed_today):
    # ОМ 600 000 from 01.10; October with a pause 15.10 - 31.10 costs 257 000 (6 lessons of 14).  05.11: 300 000 are forgiven (October and November are unpaid: debt 1 200 000).  07.11: the pause is
    # entered.  08.11: a payment of 600 000 DATED 01.11.  The world of 07.11 knew only the forgiveness (it lay on October [0; 300 000]; the room became 257 000: 43 000 cut off, alive 257 000).  The line: the
    # payment - October 257 000 + November 343 000; the forgiveness 257 000 - November.  Charged 257 000 + 600 000 = 857 000, debt 0, advance 0, used 257 000 of 300 000.
    g, s = om(world, fixed_today, joined=D(2026, 10, 1))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 7))
    pause_on(world, s, g, "2026-10-15", "2026-10-31")
    fixed_today(D(2026, 11, 8))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    check(world, s, g, D(2026, 11, 10), charged=857_000, debt=0, advance=0, used=257_000, granted=300_000)
    octo, nov = periods_of(world, s, g, D(2026, 11, 10))[:2]
    assert (octo.amount, octo.money, octo.forgiven, nov.money, nov.forgiven) == (257_000, 257_000, 0, 343_000, 257_000)


def test_b7_2_a_forgotten_payment_entered_after_the_whole_debt_was_forgiven_makes_the_forgiveness_not_needed_not_an_advance(world, fixed_today):
    # ОМ 600 000, November not paid (the student joined 02.11).  05.11: all 600 000 are forgiven.  06.11: a forgotten payment of 600 000 DATED 03.11 is entered: November is paid in full by money, the
    # forgiveness is not needed (used 0): debt 0, advance 0, refundable 0.  With a forgotten payment of 200 000: money 200 000 + forgiveness 400 000, debt 0, advance 0.
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 600_000)
    fixed_today(D(2026, 11, 6))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    check(world, s, g, D(2026, 11, 6), charged=600_000, debt=0, advance=0, used=0, granted=600_000)


def test_b7_2_a_forgotten_payment_of_a_part_is_money_and_the_forgiveness_covers_the_rest(world, fixed_today):
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 600_000)
    fixed_today(D(2026, 11, 6))
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 3))
    check(world, s, g, D(2026, 11, 6), charged=600_000, debt=0, advance=0, used=400_000, granted=600_000)


def test_b7_3_the_personal_period_a_payment_dated_before_a_forgiveness_does_not_raise_the_debt(world, fixed_today):
    # ЛП 420 000 from 01.10.  25.10: October is forgiven (420 000).  26.10: a payment of 420 000 DATED 01.10 (it paid October after all).  05.11: a payment of 420 000.  By the order of the dates the payment of
    # 01.10 closes October; the forgiveness (25.10) skips November (not begun on 25.10) and burns without holding the line; the payment of 05.11 pays November.  Today 10.11: charged 840 000, money
    # 840 000: debt 0, advance 0, used 0 of 420 000.
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1))
    fixed_today(D(2026, 10, 25))
    forgive(world, s, g, 420_000)
    fixed_today(D(2026, 10, 26))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 10, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 10))
    check(world, s, g, D(2026, 11, 10), charged=840_000, debt=0, advance=0, used=0, granted=420_000)


def _october_paused_after(world, fixed_today, forgive_day, pause_day):
    g, s = om(world, fixed_today, joined=D(2026, 10, 1))
    for day in sorted({forgive_day, pause_day}):
        fixed_today(day)
        if day == forgive_day:
            forgive(world, s, g, 600_000)
        if day == pause_day:
            pause_on(world, s, g, "2026-10-01", "2026-10-31")
    fixed_today(D(2026, 11, 10))
    return g, s


def test_b7_d_the_pause_on_the_whole_of_october_entered_AFTER_the_forgiveness(world, fixed_today):
    g, s = _october_paused_after(world, fixed_today, D(2026, 11, 5), D(2026, 11, 7))
    check(world, s, g, D(2026, 11, 10), charged=600_000, debt=600_000, advance=0, used=0, granted=600_000)


def test_b7_d_the_pause_on_the_whole_of_october_entered_BEFORE_the_forgiveness(world, fixed_today):
    g, s = _october_paused_after(world, fixed_today, D(2026, 11, 5), D(2026, 11, 4))
    check(world, s, g, D(2026, 11, 10), charged=600_000, debt=0, advance=0, used=600_000, granted=600_000)


def november_world(world, fixed_today):
    g, s = om(world, fixed_today)                           # the student joined 02.11, the clock stands on 03.11
    return g, s


def test_c5_prime_paid_300_forgiven_300_then_a_pause_takes_the_forgiveness_and_taking_the_pause_off_gives_it_back(world, fixed_today):
    # November 600 000 (12 lessons).  05.11: 300 000 are forgiven; 06.11: 300 000 are paid; 12.11: a pause 17.11 - 30.11 is entered (6 lessons: 300 000).  At the moment of the pause both lie on November (the
    # room was 600 000): the room becomes 300 000 - 300 000 are cut off the forgiveness (money first): alive 0.  Charged 300 000, money 300 000: debt 0, advance 0, used 0 of 300 000.  The pause is taken
    # off (С5): the forgiveness comes back: charged 600 000, money 300 000 + forgiveness 300 000, debt 0, used 300 000.
    g, s = november_world(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 6))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 6))
    fixed_today(D(2026, 11, 12))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 20))
    check(world, s, g, D(2026, 11, 20), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    r = owner(world).post(f"{API}/org/pauses/{pause_id}/void", json={"reason": "Выздоровел раньше"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 20), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)


def test_the_pause_to_450_000_cuts_150_000_of_the_forgiveness_in_both_orders_of_the_forgiveness_and_the_payment(world, fixed_today):
    # November 600 000; 300 000 forgiven and 300 000 paid in one order, then a pause 24.11 - 30.11 (9 lessons: 450 000): 150 000 are cut off the forgiveness: money 300 000 + forgiveness 150 000 = 450 000,
    # used 150 000 of 300 000, debt 0, advance 0.  The same when the payment comes first (05.11) and the forgiveness second (06.11).
    g, s = november_world(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 6))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 6))
    fixed_today(D(2026, 11, 12))
    pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 20))
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)


def test_the_pause_to_450_000_cuts_150_000_of_the_forgiveness_when_the_payment_came_first(world, fixed_today):
    g, s = november_world(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 6))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 12))
    pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 20))
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)


def test_the_probe_of_the_payment_window_gives_what_the_record_gives(world, fixed_today):
    # ОМ 600 000 from 01.10, October unpaid, November has begun.  05.11: 300 000 are forgiven, DATED 01.10.  07.11: a pause 01.10 - 15.10 is entered (October 7 lessons of 14: 300 000).  10.11: a payment of
    # 600 000 dated 01.10 is TRIED in the window and then recorded.  The forgiveness (alive 300 000) fills October (it was entered first); the payment goes to November.  Both say: debt 0 after.
    g, s = om(world, fixed_today, joined=D(2026, 10, 1))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000, effective_on=D(2026, 10, 1))
    fixed_today(D(2026, 11, 7))
    pause_on(world, s, g, "2026-10-01", "2026-10-15")
    fixed_today(D(2026, 11, 10))
    probe = preview(world, membership(world, s, g), amount=600_000, paid_on="2026-10-01")["result"]
    pay(world, s, g, amount=600_000, paid_on=D(2026, 10, 1))
    L = led(world, s, g, D(2026, 11, 10))
    assert (probe["unpaid_after"], probe["advance_after"]) == (L.debt, L.advance) == (0, 0)
    assert L.written_off == 300_000
