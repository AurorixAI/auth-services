"""PAY-TAILS Б11.35 п. 5 / Б11.36, step 8.14.8: М5 (the words of a pause a TRANSFER cut; the way back is a transfer back), М7 (a journal that does not end in the row, with the line of a cut: the edit is still refused and the pause is
still locked), М8 (the list of the pauses asks «the cancel of the leaving is not forbidden» once per participation).  PostgreSQL, through the routes of the product (LESSONS 135).
World (ОМ): 600 000 a month, Tue / Thu / Fri; the student since 01.11; 09.11 the pause 17 - 30.11."""
from datetime import date

from sqlalchemy import event

from tests.answers_dump import dump
from tests.money1b_helpers import add, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b14_pg import pauses_of, refused, row_of, world_cut
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import TTF, leave

pytestmark = needs_pg
D = date

TRANSFER_NOW = "Пауза закрыта переводом ученика. Переведите ученика обратно в эту группу, затем измените паузу."
TRANSFER_LATER = "Пауза закрыта переводом ученика, изменить её нельзя. Можно снять паузу целиком."
CLOSED_NOW = "Пауза закрыта уходом ученика. Верните ученика в группу, затем измените паузу."
CLOSED_LATER = "Пауза закрыта уходом ученика, изменить её нельзя. Можно снять паузу целиком."


def transferred(world, fixed_today):
    """09.11 the pause 17 - 30 in the group; 19.11 the student is TRANSFERRED to the group Бета (the old pause is cut to 17 - 19)."""
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    b = make_group(world.db, world.org, world.teacher, "Бета")
    give_group_price(world.db, b, 600_000)
    set_schedule(world, TTF, b)
    ensure_billing(world, b, price=None)
    fixed_today(D(2026, 11, 9))
    old_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    add(world, s, group=b, mode="transfer", from_group_id=str(g.id))
    return g, s, b, old_id


def test_b11_36_5_the_pause_a_transfer_cut_the_day_of_the_transfer_says_transfer_him_back_and_that_is_true(world, fixed_today):
    g, s, b, old_id = transferred(world, fixed_today)
    refused(world, s, g, D(2026, 11, 19), old_id, TRANSFER_NOW)                                      # the 409 of the probe and of the save
    assert [(p["id"], p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g)] == [(old_id, True, TRANSFER_NOW)]          # the window says the same words
    dump("pause_closed_by_transfer_today", pauses_of(world, s, g)[0])
    back = add(world, s, group=g, mode="transfer", from_group_id=str(b.id))["leave_undo"]               # the way back the phrase names: the transfer back cancels the leaving
    assert back["undone"] is True, back
    assert row_of(world, old_id) == (D(2026, 11, 17), D(2026, 11, 30), False)                          # the pause is given back whole
    assert [(p["id"], p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g)] == [(old_id, False, None)]


def test_b11_36_5_the_pause_a_transfer_cut_a_day_later_says_it_cannot_be_edited(world, fixed_today):
    g, s, b, old_id = transferred(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    refused(world, s, g, D(2026, 11, 20), old_id, TRANSFER_LATER)
    assert [(p["id"], p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g)] == [(old_id, True, TRANSFER_LATER)]
    dump("pause_closed_by_transfer_later", pauses_of(world, s, g)[0])


def test_b11_36_5_a_leaving_and_an_archive_keep_their_two_phrases(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)                                                        # the leaving of today
    refused(world, s, g, D(2026, 11, 19), pause_id, CLOSED_NOW)
    fixed_today(D(2026, 11, 20))
    refused(world, s, g, D(2026, 11, 20), pause_id, CLOSED_LATER)


def test_b11_36_3_a_journal_that_does_not_end_in_the_row_but_has_the_line_of_a_cut_still_locks_the_pause(world, fixed_today):
    # The core reads such a pause as it is (known always: Б11.36 п. 2); the edit and the window look at the PRESENCE of the line of the cut: the days stay closed (409 ``PAUSE_CLOSED_BY_LEAVE`` in the probe and in the save,
    # ``days_locked`` true).  The row is changed BY HAND here - the only way to a journal nobody wrote (no route of the product makes it).
    g, s, pause_id = world_cut(world, fixed_today)
    from uuid import UUID
    from src.models.auth import MembershipPause
    row = world.db.get(MembershipPause, UUID(pause_id))
    row.last_day = D(2026, 11, 18)                                                                         # the journal says 17 - 19, the row 17 - 18
    world.db.commit()
    refused(world, s, g, D(2026, 11, 19), pause_id, CLOSED_NOW)
    assert [(p["id"], p["days_locked"]) for p in pauses_of(world, s, g)] == [(pause_id, True)]


def statements(world, call):
    """The number of the SELECTs the call makes (the engine of the test session)."""
    engine = world.db.get_bind()
    counted = []
    listener = lambda conn, cursor, statement, params, context, executemany: counted.append(statement) if statement.lstrip().upper().startswith("SELECT") else None
    event.listen(engine, "before_cursor_execute", listener)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return len(counted)


def two_cuts(world, fixed_today, *, both):
    """05.11..: the pause A 17 - 30 cut by the leaving of 19.11 (ordinary return 21.11); the pause B 22 - 30 (entered 22.11) cut by the leaving of 23.11 (the one that stands).  ``both`` False: no A."""
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 9))
    if both:
        pause_on(world, s, g, "2026-11-17", "2026-11-30")
        fixed_today(D(2026, 11, 19))
        leave(world, g, s)
        fixed_today(D(2026, 11, 21))
        add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 22))
    pause_on(world, s, g, "2026-11-22", "2026-11-30")
    fixed_today(D(2026, 11, 23))
    leave(world, g, s)
    return g, s


def test_b11_36_7_the_list_asks_the_cancel_of_the_leaving_once_for_the_participation_two_closed_pauses_cost_what_one_costs(world, fixed_today):
    g, s = two_cuts(world, fixed_today, both=True)
    listed = lambda: owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses")
    r = listed()
    assert r.status_code == 200, r.text
    locked = [(p["first_day"], p["last_day"], p["days_locked_text"]) for p in r.json() if p["days_locked"]]
    assert locked == [("2026-11-22", "2026-11-23", CLOSED_NOW), ("2026-11-17", "2026-11-19", CLOSED_LATER)], r.json()          # the cut of the LAST leaving is asked of every pause: the older one says «later»
    two = statements(world, listed)
    old = next(p for p in r.json() if p["first_day"] == "2026-11-17")
    gone = owner(world).post(f"{API}/org/pauses/{old['id']}/void", json={"reason": "Передумали"})                   # the older closed pause is taken off: ONE closed pause is left
    assert gone.status_code == 200, gone.text
    one = statements(world, listed)
    assert [p["days_locked"] for p in listed().json() if p["voided_at"] is None] == [True]
    print("M8 SELECTs of the list: two closed pauses / one closed pause:", two, one)
    assert two == one, ("SELECTs of the list: two closed pauses / one closed pause", two, one)
