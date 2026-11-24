"""PAY-TAILS Б11.30, step 8.14: the pause by the PARTICIPATION and by the DAYS.  PostgreSQL, through the routes of the product (LESSONS 135).  Every edit, take-off and entering of a pause checks the FOUR numbers of lesson 168:
the probe (``dry_run``), ``impact`` of the answer, ``row`` of the answer, the account after (and its charge / used forgiveness).  The numbers are those of the spec (Б11.30 п. 4); a number the spec does not give (the middle step
of a world) is counted by the clean ledger with a copy of the definition on sets of days (``tests/pay_tails_b7_tools.py`` ``known_pauses``), written in the comment.

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1; November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each; the student since 01.11.
Credits K: 05.11 paid 150 000; 09.11 the pause 17 - 30.11 (November 300 000); 10.11 forgiven 100 000; 11.11 paid 300 000 -> used 100 000, advance 250 000.
Credits K3 (no payment on 05.11): 09.11 the pause 17 - 30.11; 10.11 forgiven 300 000; 11.11 paid 300 000 -> used 300 000, advance 300 000."""
from datetime import date
from uuid import UUID

from tests.answers_dump import dump
from tests.money1b_helpers import add, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b13_pg import edit_pause, leave_numbers
from tests.test_pay_tails_as_is_pg import chain_2_world
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import TTF, check, forgive, leave, led
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date

CLOSED_NOW = "Пауза закрыта уходом ученика. Верните ученика в группу, затем измените паузу."
CLOSED_LATER = "Пауза закрыта уходом ученика, изменить её нельзя. Можно снять паузу целиком."
TRANSFER_NOW = "Пауза закрыта переводом ученика. Переведите ученика обратно в эту группу, затем измените паузу."          # Б11.36 п. 5: a pause a TRANSFER cut


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


def test_b11_30_d3_the_world_of_the_freeze_taking_a_pause_off_and_entering_a_longer_one_is_the_edit_of_the_pause(world, fixed_today):
    # The test «as it is» of step 8.9 moved here with the number the rule gives (the ONLY change of an expectation of this step): the student since 01.11; 02.11 a pause 24 - 30.11; 05.11 the whole 450 000 forgiven; 10.11 the parent
    # pays 600 000.  12.11 the pause is TAKEN OFF and a new one 17 - 30.11 is entered: the final days are the days of the edit 24 - 30 -> 17 - 30 (the sibling test of the as-it-is file): November 300 000, the forgiveness
    # keeps 300 000 (used 300 000), the money is not touched: advance 600 000 (was: used 150 000, advance 450 000, a debt of 150 000 from 01.12).
    g, s = chain_2_world(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    pause = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses").json()[0]["id"]
    assert owner(world).post(f"{API}/org/pauses/{pause}/void", json={"reason": "Поставили не так"}).status_code == 200
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=600_000, used=300_000, granted=450_000)


def test_b11_30_d4_splitting_a_pause_into_two_is_no_edit(world, fixed_today):
    # K3; 12.11 the pause 17 - 23.11 (450 000, used 300 000, advance 150 000); 13.11 a new pause 24 - 30.11: the final days are 17 - 30: 300 000 / used 300 000 / advance 300 000.  (Was: used 150 000, advance 150 000.)
    g, s, pause_id = world_k3(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-17", "2026-11-23", debt=0, advance=150_000, charged=450_000, used=300_000, granted=300_000)
    fixed_today(D(2026, 11, 13))
    new_pause(world, s, g, D(2026, 11, 13), "2026-11-24", "2026-11-30", debt=0, advance=300_000, charged=300_000, used=300_000, granted=300_000)


# ---------------------------------------------------------------------------------------------------------------------------- W3 (the reachable form): the new pause after the return, the second leaving, its cancel
def world_w3(world, fixed_today):
    # 05.11 paid 150 000; 09.11 the pause 17 - 30.11; 19.11 the leaving (the cut 17 - 19.11); 21.11 an ORDINARY return (the lesson of 20.11 is not his); 21.11 forgiven 250 000 (of the debt 300 000) and paid 200 000: the stay
    # 01 - 19.11 and from 21.11, the pause 17 - 19: the lessons 3 5 6 10 12 13 + 24 26 27 = 9 x 50 000 = 450 000, used 250 000, advance 150 000; 22.11 a NEW pause 24 - 30.11 (the human enters it: the days 24 - 30 were a pause by
    # the old pause in the worlds before the leaving): the lessons 3 5 6 10 12 13 = 300 000, the forgiveness is cut by 150 000: used 100 000, advance 150 000, debt 0 (the clean ledger with the definition).
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
    return g, s, new_id


def test_b11_30_w3_a_new_pause_after_an_ordinary_return_is_counted_by_the_days_of_the_old_pause(world, fixed_today):
    world_w3(world, fixed_today)


def test_b11_30_w3_the_second_leaving_makes_the_stay_empty_and_drops_it_from_the_input_its_cancel_gives_it_back(world, fixed_today):
    # 23.11 the SECOND leaving: the stay 21 - 23.11 has no lesson of its own (24 26 27 are the days of the pause 24 - 30.11 that goes with the cut), so it never began and leaves no trace (Б11.14: the records made during it
    # count as made after the previous leaving).  The real stay 01 - 19.11 costs 6 lessons x 50 000 = 300 000; the first payment 150 000; the forgiveness covers the whole debt 150 000 left (used 150 000); the payment of 200 000
    # made after the return is to give back whole: advance 200 000 = the limit of the refund.  The four numbers (the window of the leaving, the answer, the row, the account after) are one set; the cancel the same day gives
    # the stay back: used 100 000, advance 150 000 (the same as before the leaving).
    from tests.payf_helpers import rows
    g, s, new_id = world_w3(world, fixed_today)
    fixed_today(D(2026, 11, 23))
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    window = owner(world).get(f"{url}/leave-preview")
    assert window.status_code == 200, window.text
    probe = next(m for m in window.json()["memberships"] if m["membership_id"] == str(membership(world, s, g).id))["money"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    after = led(world, s, g, D(2026, 11, 23))
    stays = done.json()["effects"]["stays"]
    row = next(r for r in rows(world, s) if r["membership_id"] == str(membership(world, s, g).id))
    assert [(x["code"], x["amount"]) for x in stays] == [("advance", 200_000)] and (row["debt_amount"], row["state"], row["state_text"]) == (0, "paid", "Аванс 200\xa0000\xa0сум"), (stays, row)
    assert (probe["charged_after_leave"], probe["debt_amount"], probe["advance"], probe["refundable"]) == (after.charged, after.debt, after.advance, after.refundable) == (300_000, 0, 200_000, 200_000), (probe, after)
    assert (after.written_off, after.refundable) == (150_000, 200_000)
    check(world, s, g, D(2026, 11, 23), charged=300_000, debt=0, advance=200_000, used=150_000, granted=250_000)
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
    refused(world, s, g, D(2026, 11, 19), old_id, TRANSFER_NOW)          # Б11.36 п. 5: a transfer, not a leaving: the way back is the transfer back (it was «a leaving ... cannot be edited»)
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


# ---------------------------------------------------------------------------------------------------------------------------- Б11.33 п. 1: the pause says itself that its days are closed
def pauses_of(world, s, g):
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses")
    assert r.status_code == 200, r.text
    return r.json()


def test_b11_33_the_pause_without_a_cut_is_not_locked_in_the_list_and_in_the_answers_of_the_routes(world, fixed_today):
    g, s, pause_id = world_k(world, fixed_today)
    assert [(p["id"], p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g)] == [(pause_id, False, None)]
    dump("pause_without_cut", pauses_of(world, s, g)[0])
    body = {"first_day": "2026-11-13", "last_day": "2026-11-30", "reason": "Поправили"}
    for params in ({"dry_run": "true"}, {}):
        r = owner(world).patch(f"{API}/org/pauses/{pause_id}", params=params, json=body)
        assert r.status_code == 200 and (r.json()["pause"]["days_locked"], r.json()["pause"]["days_locked_text"]) == (False, None), r.text
    other = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-12-03", "last_day": "2026-12-05", "reason": "Поездка"})
    assert other.status_code in (200, 201) and (other.json()["pause"]["days_locked"], other.json()["pause"]["days_locked_text"]) == (False, None), other.text
    voided = owner(world).post(f"{API}/org/pauses/{other.json()['pause']['id']}/void", json={"reason": "Передумали"})
    assert voided.status_code == 200 and (voided.json()["pause"]["days_locked"], voided.json()["pause"]["days_locked_text"]) == (False, None), voided.text


def test_b11_33_the_pause_under_a_cut_says_the_phrase_of_the_refusal_by_the_state_and_the_cancel_of_the_leaving_gives_it_back(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)                                     # the leaving of 19.11 cut the pause 17 - 30.11 to 17 - 19.11
    seen = lambda: [(p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g) if p["id"] == pause_id]
    mine = lambda: [p for p in pauses_of(world, s, g) if p["id"] == pause_id][0]
    assert seen() == [(True, CLOSED_NOW)]                                              # the leaving is of today and can be cancelled
    dump("pause_closed_by_leave_today", mine())
    body = {"reason": "Другая причина"}                                               # the reason alone: 200, the days stay locked, the answer says so
    for params in ({"dry_run": "true"}, {}):
        r = owner(world).patch(f"{API}/org/pauses/{pause_id}", params=params, json=body)
        assert r.status_code == 200 and (r.json()["pause"]["days_locked"], r.json()["pause"]["days_locked_text"]) == (True, CLOSED_NOW), r.text
    refused(world, s, g, D(2026, 11, 19), pause_id, CLOSED_NOW)                        # the same phrase is the one of the 409
    fixed_today(D(2026, 11, 20))
    assert seen() == [(True, CLOSED_LATER)]
    dump("pause_closed_by_leave_later", mine())
    refused(world, s, g, D(2026, 11, 20), pause_id, CLOSED_LATER)
    fixed_today(D(2026, 11, 19))                                                       # a leaving of today again, then its cancel: the pause has its days back, nothing is locked
    assert seen() == [(True, CLOSED_NOW)]
    assert add(world, s, group=g, mode="add")["leave_undo"]["undone"] is True
    assert row_of(world, pause_id) == (D(2026, 11, 17), D(2026, 11, 30), False)
    assert seen() == [(False, None)]
    dump("pause_after_the_leaving_is_cancelled", mine())


def test_b11_33_the_pause_a_transfer_carried_is_not_locked_the_one_it_cut_is(world, fixed_today):
    # 09.11 the pause 17 - 30.11; 19.11 a transfer to Бета: the old pause is cut to 17 - 19 (locked, the phrase of a later day), the carried 20 - 30 is the pause of the new group: not locked, no phrase.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    b = make_group(world.db, world.org, world.teacher, "Бета")
    give_group_price(world.db, b, 600_000)
    set_schedule(world, TTF, b)
    ensure_billing(world, b, price=None)
    fixed_today(D(2026, 11, 9))
    old_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    add(world, s, group=b, mode="transfer", from_group_id=str(g.id))
    assert [(p["id"], p["days_locked"], p["days_locked_text"]) for p in pauses_of(world, s, g)] == [(old_id, True, TRANSFER_NOW)]
    carried = pauses_of(world, s, b)
    assert [(p["first_day"], p["last_day"], p["days_locked"], p["days_locked_text"]) for p in carried] == [("2026-11-20", "2026-11-30", False, None)]


# ---------------------------------------------------------------------------------------------------------------------------- the leaving and the edit of the days of a pause at the same time
# What the two routes lock (read in the code, then tested): the edit takes the pause row FOR UPDATE (``_locked_pause``) and the membership FOR NO KEY UPDATE; the leaving takes the student / the membership FOR KEY SHARE (it
# does NOT wait for an edit by those) and, since this step, the pause rows it cuts FOR UPDATE (``_live(lock=True)`` in ``close_at_leave``: the edit in flight is waited for and the cut is made of the form it wrote).  The edit
# reads the journal of the pause AFTER its row lock: a leaving that came first is seen (409).  A real concurrent pair is not built on the routes (one session of the test): the two ORDERS are run one after the other, and the
# locks are asserted on the statements.
def test_b11_30_race_the_edit_first_then_the_leaving_cuts_the_form_the_edit_wrote(world, fixed_today):
    # 09.11 the pause 17 - 30.11; 18.11 the edit to 15 - 30.11 (no cut stands: allowed); 19.11 the leaving: the cut 15 - 19.11 with ``was`` = 15 - 30.11; November 3 5 6 10 12 13 = 300 000 (the lessons 17 and 19 are paused).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 18))
    r = owner(world).patch(f"{API}/org/pauses/{pause_id}", json={"first_day": "2026-11-15", "last_day": "2026-11-30", "reason": "Поправили"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 19))
    leave(world, g, s)
    assert row_of(world, pause_id) == (D(2026, 11, 15), D(2026, 11, 19), False)
    assert journal_of(world, pause_id)[-1] == ("membership_pause_set", "2026-11-15", "2026-11-19", "leave")
    from src.models.auth import GroupAudit
    cut = [a for a in world.db.query(GroupAudit).order_by(GroupAudit.created_at, GroupAudit.id) if (a.snapshot or {}).get("pause_id") == pause_id and (a.snapshot or {}).get("via") == "leave"][0]
    assert cut.snapshot["was"] == ["2026-11-15", "2026-11-30"]
    assert led(world, s, g, D(2026, 11, 19)).charged == 300_000


def test_b11_30_race_the_leaving_first_then_the_edit_is_refused_and_nothing_is_written(world, fixed_today):
    g, s, pause_id = world_cut(world, fixed_today)                       # 19.11 the leaving cuts 17 - 30 to 17 - 19
    refused(world, s, g, D(2026, 11, 19), pause_id, CLOSED_NOW)


def test_b11_30_race_the_leaving_locks_the_pause_rows_it_cuts(world, fixed_today):
    from sqlalchemy import event
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 9))
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 19))
    seen = []
    engine = world.db.get_bind()
    spy = lambda conn, cursor, statement, params, context, executemany: seen.append(statement)
    event.listen(engine, "before_cursor_execute", spy)
    try:
        leave(world, g, s)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    selects = [x for x in seen if "FROM membership_pauses" in x and x.lstrip().upper().startswith("SELECT")]
    assert selects and any("FOR UPDATE" in x for x in selects), selects
