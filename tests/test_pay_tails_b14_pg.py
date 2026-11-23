"""PAY-TAILS Б11.30, step 8.14: the pause by the PARTICIPATION and by the DAYS.  PostgreSQL, through the routes of the product (LESSONS 135).  Every edit, take-off and entering of a pause checks the FOUR numbers of lesson 168:
the probe (``dry_run``), ``impact`` of the answer, ``row`` of the answer, the account after (and its charge / used forgiveness).  The numbers are those of the spec (Б11.30 п. 4); a number the spec does not give (the middle step
of a world) is counted by the clean ledger with a copy of the definition on sets of days (``tests/pay_tails_b7_tools.py`` ``known_pauses``), written in the comment.

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1; November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each; the student since 01.11.
Credits K: 05.11 paid 150 000; 09.11 the pause 17 - 30.11 (November 300 000); 10.11 forgiven 100 000; 11.11 paid 300 000 -> used 100 000, advance 250 000.
Credits K3 (no payment on 05.11): 09.11 the pause 17 - 30.11; 10.11 forgiven 300 000; 11.11 paid 300 000 -> used 300 000, advance 300 000."""
from datetime import date
from uuid import UUID

from tests.money1b_helpers import add, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b13_pg import edit_pause, leave_numbers
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import TTF, check, forgive, leave, led
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date

CLOSED_NOW = "Пауза закрыта уходом ученика. Верните ученика в группу, затем измените паузу."
CLOSED_LATER = "Пауза закрыта уходом ученика, изменить её нельзя. Можно снять паузу целиком."


def four(world, s, g, probe, saved, today, *, debt, advance, charged, used, granted):
    """The four numbers: the probe, ``impact`` of the answer, ``row`` of the answer, the account after - and the charge and the used forgiveness of the account."""
    assert probe.status_code in (200, 201) and saved.status_code in (200, 201), (probe.text, saved.text)
    said = lambda i: (i["debt_after"], i["advance_after"])
    row, after = saved.json()["row"], led(world, s, g, today)
    assert said(probe.json()["impact"]) == said(saved.json()["impact"]) == (row["unpaid"], row["advance"]) == (after.debt, after.advance) == (debt, advance), (
        'GOT charged/used', after.charged, after.written_off, 'probe/impact/row/after (debt, advance)', said(probe.json()["impact"]), said(saved.json()["impact"]), (row["unpaid"], row["advance"]), (after.debt, after.advance))
    check(world, s, g, today, charged=charged, debt=debt, advance=advance, used=used, granted=granted)


def new_pause(world, s, g, today, first, last, **expect):
    body = {"first_day": first, "last_day": last, "reason": "Болезнь"}
    url = f"{API}/org/memberships/{membership(world, s, g).id}/pauses"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    saved = owner(world).post(url, json=body)
    four(world, s, g, probe, saved, today, **expect)
    return saved.json()["pause"]["id"]


def void_pause(world, s, g, today, pause_id, **expect):
    body = {"reason": "Передумали"}
    probe = owner(world).post(f"{API}/org/pauses/{pause_id}/void", params={"dry_run": "true"}, json=body)
    saved = owner(world).post(f"{API}/org/pauses/{pause_id}/void", json=body)
    four(world, s, g, probe, saved, today, **expect)


def world_k(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 10))
    forgive(world, s, g, 100_000)
    fixed_today(D(2026, 11, 11))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 11))
    check(world, s, g, D(2026, 11, 11), charged=300_000, debt=0, advance=250_000, used=100_000, granted=100_000)
    return g, s, pause_id


def world_k3(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 10))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 11))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 11))
    check(world, s, g, D(2026, 11, 11), charged=300_000, debt=0, advance=300_000, used=300_000, granted=300_000)
    return g, s, pause_id


# ---------------------------------------------------------------------------------------------------------------------------- Д1 - Д4: the same days = the same account, whatever buttons led to them
def test_b11_30_d1_two_edits_of_a_pause_are_one_edit(world, fixed_today):
    # K; 12.11 the pause is 13 - 25.11: November 350 000 (lessons 3 5 6 10 12 26 27), used 50 000, advance 150 000 (the clean ledger with the definition); 13.11 it is 13 - 30.11: the lessons 13 17 19 20 24 26 27 are lost,
    # 3 5 6 10 12 = 250 000; the forgiveness of 10.11 saw the pause 17 - 30 (the days 17 - 30 are in the final form): used 50 000, advance 250 000.  (Was: used 0, advance 200 000.)
    g, s, pause_id = world_k(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-13", "2026-11-25", debt=0, advance=150_000, charged=350_000, used=50_000, granted=100_000)
    fixed_today(D(2026, 11, 13))
    edit_pause(world, s, g, pause_id, D(2026, 11, 13), "2026-11-13", "2026-11-30", debt=0, advance=250_000, charged=250_000, used=50_000, granted=100_000)


def test_b11_30_d1_the_same_by_one_edit(world, fixed_today):
    g, s, pause_id = world_k(world, fixed_today)
    fixed_today(D(2026, 11, 13))
    edit_pause(world, s, g, pause_id, D(2026, 11, 13), "2026-11-13", "2026-11-30", debt=0, advance=250_000, charged=250_000, used=50_000, granted=100_000)


def test_b11_30_d2_shorten_and_bring_back_is_no_edit(world, fixed_today):
    # K3; 12.11 the pause 17 - 23.11 (24 26 27 are charged: 450 000 = the money 300 000 + the forgiveness 300 000 -> used 300 000, advance 150 000); 13.11 back to 17 - 30.11: as without the two edits.
    g, s, pause_id = world_k3(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-17", "2026-11-23", debt=0, advance=150_000, charged=450_000, used=300_000, granted=300_000)
    fixed_today(D(2026, 11, 13))
    edit_pause(world, s, g, pause_id, D(2026, 11, 13), "2026-11-17", "2026-11-30", debt=0, advance=300_000, charged=300_000, used=300_000, granted=300_000)


def test_b11_30_d3_taking_a_pause_off_and_entering_the_same_days_again_is_no_edit(world, fixed_today):
    # K3; 12.11 the human takes the pause off: it never was (С5): 600 000 = the money 300 000 + the forgiveness 300 000, used 300 000, advance 0; 13.11 a NEW pause 17 - 30.11: the days were a pause when the forgiveness
    # was entered (the pause the human took off had them): 300 000 / used 300 000 / advance 300 000.  (Was: used 0, advance 0.)
    g, s, pause_id = world_k3(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    void_pause(world, s, g, D(2026, 11, 12), pause_id, debt=0, advance=0, charged=600_000, used=300_000, granted=300_000)
    fixed_today(D(2026, 11, 13))
    new_pause(world, s, g, D(2026, 11, 13), "2026-11-17", "2026-11-30", debt=0, advance=300_000, charged=300_000, used=300_000, granted=300_000)


def test_b11_30_d4_splitting_a_pause_into_two_is_no_edit(world, fixed_today):
    # K3; 12.11 the pause 17 - 23.11 (450 000, used 300 000, advance 150 000); 13.11 a new pause 24 - 30.11: the final days are 17 - 30: 300 000 / used 300 000 / advance 300 000.  (Was: used 150 000, advance 150 000.)
    g, s, pause_id = world_k3(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-17", "2026-11-23", debt=0, advance=150_000, charged=450_000, used=300_000, granted=300_000)
    fixed_today(D(2026, 11, 13))
    new_pause(world, s, g, D(2026, 11, 13), "2026-11-24", "2026-11-30", debt=0, advance=300_000, charged=300_000, used=300_000, granted=300_000)


# ---------------------------------------------------------------------------------------------------------------------------- W3 (the reachable form): the new pause after the return, the second leaving, its cancel
def test_b11_30_w3_a_new_pause_after_an_ordinary_return_the_second_leaving_takes_it_off_and_its_cancel_gives_it_back(world, fixed_today):
    # 05.11 paid 150 000; 09.11 the pause 17 - 30.11; 19.11 the leaving (the cut 17 - 19.11); 21.11 an ORDINARY return (the lesson of 20.11 is not his); 21.11 forgiven 250 000 (of the debt 300 000) and paid 200 000: the stay
    # 01 - 19.11 and from 21.11, the pause 17 - 19: the lessons 3 5 6 10 12 13 17? no - 17 19 are paused: 3 5 6 10 12 13 + 24 26 27 = 9 x 50 000 = 450 000, used 250 000, advance 150 000; 22.11 a NEW pause 24 - 30.11 (the
    # human enters it: the days 24 - 30 were a pause by the old pause in the worlds before the leaving): the lessons 3 5 6 10 12 13 = 300 000, the forgiveness is cut by 150 000: used 100 000, advance 150 000, debt 0;
    # 23.11 the SECOND leaving (it takes the pause of the future off: the stay has no lesson after 23.11 outside the pause): the same numbers; its cancel the same day: the same numbers.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    leave(world, g, s)
    fixed_today(D(2026, 11, 21))
    add(world, s, group=g, mode="add")
    forgive(world, s, g, 250_000)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 21))
    check(world, s, g, D(2026, 11, 21), charged=450_000, debt=0, advance=150_000, used=250_000, granted=250_000)
    fixed_today(D(2026, 11, 22))
    new_id = new_pause(world, s, g, D(2026, 11, 22), "2026-11-24", "2026-11-30", debt=0, advance=150_000, charged=300_000, used=100_000, granted=250_000)
    fixed_today(D(2026, 11, 23))
    leave_numbers(world, s, g, D(2026, 11, 23), (300_000, 0, 150_000, 100_000, 150_000))
    check(world, s, g, D(2026, 11, 23), charged=300_000, debt=0, advance=150_000, used=100_000, granted=250_000)
    back = add(world, s, group=g, mode="add")["leave_undo"]
    assert back["undone"] is True, back
    check(world, s, g, D(2026, 11, 23), charged=300_000, debt=0, advance=150_000, used=100_000, granted=250_000)
    from src.models.auth import MembershipPause
    row = world.db.get(MembershipPause, UUID(new_id))
    assert row.voided_at is None and (row.first_day, row.last_day) == (D(2026, 11, 24), D(2026, 11, 30))


# ---------------------------------------------------------------------------------------------------------------------------- the refusals: the days of a pause a leaving cut are not edited
def journal_of(world, pause_id):
    """The lines of the journal of the pause: ``[(action, first_day, last_day, via)]``."""
    from src.models.auth import GroupAudit
    return [(a.action, a.snapshot.get("first_day"), a.snapshot.get("last_day"), a.snapshot.get("via"))
            for a in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id).order_by(GroupAudit.created_at, GroupAudit.id) if (a.snapshot or {}).get("pause_id") == pause_id]


def row_of(world, pause_id):
    from src.models.auth import MembershipPause
    world.db.expire_all()
    r = world.db.get(MembershipPause, UUID(pause_id))
    return r.first_day, r.last_day, r.voided_at is not None


def refused(world, s, g, today, pause_id, text, first="2026-11-13", last="2026-11-30"):
    """The probe and the save of the days are refused (409 ``PAUSE_CLOSED_BY_LEAVE``, the text by the state); the account, the row and the journal of the pause are as before."""
    before = (led(world, s, g, today).charged, led(world, s, g, today).debt, led(world, s, g, today).advance, led(world, s, g, today).written_off)
    lines, row = journal_of(world, pause_id), row_of(world, pause_id)
    for params in ({"dry_run": "true"}, {}):
        r = owner(world).patch(f"{API}/org/pauses/{pause_id}", params=params, json={"first_day": first, "last_day": last, "reason": "Поправили"})
        assert r.status_code == 409, (params, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "PAUSE_CLOSED_BY_LEAVE" and r.json()["detail"]["message"] == text, r.json()
    after = led(world, s, g, today)
    assert (after.charged, after.debt, after.advance, after.written_off) == before
    assert journal_of(world, pause_id) == lines and row_of(world, pause_id) == row


def world_cut(world, fixed_today, leave_day=19):
    # 05.11 paid 150 000; 09.11 the pause 17 - 30.11; the leaving on ``leave_day`` cuts it to 17 - 19.11 (or less)
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, leave_day))
    leave(world, g, s)
    return g, s, pause_id


def test_b11_30_refusal_the_leaving_of_today_the_text_says_to_bring_the_student_back(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)
    assert row_of(world, pause_id) == (D(2026, 11, 17), D(2026, 11, 19), False)
    refused(world, s, g, D(2026, 11, 19), pause_id, CLOSED_NOW)


def test_b11_30_refusal_the_leaving_of_a_day_ago_the_text_says_it_cannot_be_edited(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    refused(world, s, g, D(2026, 11, 20), pause_id, CLOSED_LATER)


def test_b11_30_refusal_after_an_ordinary_return(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)
    fixed_today(D(2026, 11, 21))
    add(world, s, group=g, mode="add")
    refused(world, s, g, D(2026, 11, 21), pause_id, CLOSED_LATER)


def test_b11_30_refusal_after_a_transfer_and_the_carried_pause_of_the_new_group_is_edited(world, fixed_today):
    # 09.11 the pause 17 - 30.11 in the group; 19.11 the student is TRANSFERRED to the group Бета: the pause of the old group is cut to 17 - 19 (closed: the days are not edited), the rest 20 - 30 goes with him as a
    # pause of the new group (the line of its entering has ``via`` transfer and no ``was``: it is NOT a cut) - it is edited as any pause.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    b = make_group(world.db, world.org, world.teacher, "Бета")
    give_group_price(world.db, b, 600_000)
    set_schedule(world, TTF, b)
    ensure_billing(world, b, price=None)
    fixed_today(D(2026, 11, 9))
    old_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    add(world, s, group=b, mode="transfer", from_group_id=str(g.id))
    assert row_of(world, old_id) == (D(2026, 11, 17), D(2026, 11, 19), False)
    refused(world, s, g, D(2026, 11, 19), old_id, CLOSED_LATER)
    from src.models.auth import MembershipPause
    carried = world.db.query(MembershipPause).filter(MembershipPause.membership_id == membership(world, s, b).id, MembershipPause.voided_at.is_(None)).one()
    assert (carried.first_day, carried.last_day) == (D(2026, 11, 20), D(2026, 11, 30))
    body = {"first_day": "2026-11-23", "last_day": "2026-11-30", "reason": "Поправили"}
    probe = owner(world).patch(f"{API}/org/pauses/{carried.id}", params={"dry_run": "true"}, json=body)
    saved = owner(world).patch(f"{API}/org/pauses/{carried.id}", json=body)
    assert probe.status_code == 200 and saved.status_code == 200, (probe.text, saved.text)
    after = led(world, s, b, D(2026, 11, 19))
    said = lambda i: (i["debt_after"], i["advance_after"])
    assert said(probe.json()["impact"]) == said(saved.json()["impact"]) == (saved.json()["row"]["unpaid"], saved.json()["row"]["advance"]) == (after.debt, after.advance)


def test_b11_30_refusal_after_the_archive_of_the_student(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    r = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
    assert r.status_code == 200, r.text
    assert row_of(world, pause_id) == (D(2026, 11, 17), D(2026, 11, 19), False)
    fixed_today(D(2026, 11, 20))
    refused(world, s, g, D(2026, 11, 20), pause_id, CLOSED_LATER)


def test_b11_30_the_reason_alone_of_a_closed_pause_is_edited_and_a_pause_the_leaving_did_not_touch_is_edited_as_before(world, fixed_today):
    # the reason: the days are the same, 200, the account does not move.  A pause 03 - 05.11 (before the day of the leaving 19.11) is not cut: it is edited as before, also of a student who left.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 2))
    early = pause_on(world, s, g, "2026-11-03", "2026-11-05")
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    leave(world, g, s)
    before = led(world, s, g, D(2026, 11, 19))
    r = owner(world).patch(f"{API}/org/pauses/{pause_id}", json={"reason": "Другая причина"})
    assert r.status_code == 200, r.text
    assert row_of(world, pause_id) == (D(2026, 11, 17), D(2026, 11, 19), False)
    assert (led(world, s, g, D(2026, 11, 19)).charged, led(world, s, g, D(2026, 11, 19)).debt) == (before.charged, before.debt)
    body = {"first_day": "2026-11-03", "last_day": "2026-11-06", "reason": "Поправили"}
    probe = owner(world).patch(f"{API}/org/pauses/{early}", params={"dry_run": "true"}, json=body)
    saved = owner(world).patch(f"{API}/org/pauses/{early}", json=body)
    assert probe.status_code == 200 and saved.status_code == 200, (probe.text, saved.text)
    after = led(world, s, g, D(2026, 11, 19))
    said = lambda i: (i["debt_after"], i["advance_after"])
    assert said(probe.json()["impact"]) == said(saved.json()["impact"]) == (saved.json()["row"]["unpaid"], saved.json()["row"]["advance"]) == (after.debt, after.advance)
