"""Money-2 / wave A (transfer, leaving, return - the server): T1..T9 (PostgreSQL).  Every money number is worked out by hand in the comment of its test."""
from datetime import date, datetime, timedelta

import pytest

from src.models.auth import (
    GroupAudit, MembershipPause, MembershipPriceOverride, MembershipTransfer, OrganizationMember, StudentGroup, StudentPayment, StudentPaymentFile,
)

from src.auth import payments_texts as tx
from tests.money1b_helpers import account, add, set_schedule, slot
from tests.pg_world import business_today, ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

DAY = date(2026, 11, 20)            # Friday
DAILY = [slot(f"d{n}", n, "2026-01-01") for n in range(1, 8)]          # one lesson every day: a month of 30 days has 30 lessons


def groups3(w, fixed_today, today=DAY):
    """Three groups of one centre (A = the group of the world, B, C), prices 300 000 / 400 000 / 500 000 a month, calendar months from the 1st (every group has a lesson every day).  Returns ``(A, B, C)``."""
    fixed_today(today)
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    ensure_billing(w)
    give_group_price(w.db, w.group, 300_000)
    b, c = make_group(w.db, w.org, w.teacher, "Бета"), make_group(w.db, w.org, w.teacher, "Гамма")
    give_group_price(w.db, b, 400_000)
    give_group_price(w.db, c, 500_000)
    for group in (w.group, b, c):
        set_schedule(w, DAILY, group)
    return w.group, b, c


def snap(w, student, group):
    """Everything about one membership that a transfer of ANOTHER group must not move: the ledger, the stay, the pauses, the prices (the money that went away and came back is net zero, both links stay: a transfer is never cancelled)."""
    m = membership(w, student, group)
    led = account(w, m.id)
    pauses = sorted((p.first_day, p.last_day, p.voided_at is None) for p in w.db.query(MembershipPause).filter_by(membership_id=m.id))
    prices = sorted((r.valid_from, r.amount, r.reason) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id))
    return (m.is_active, m.left_at, led.charged, led.advance, led.debt, led.covered_through, led.transferred_in - led.transferred_out, tuple(pauses), tuple(prices))


# ---------------------------------------------------------------------------------------------------- T1 one source


def test_t1_a_transfer_closes_only_the_named_source_and_the_other_group_is_untouched(world, fixed_today):
    # Alia is in A (300 000) and C (500 000); she pays 600 000 into C.  Transferring A -> B must not change C by one sum or one day (I1).
    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    add(world, s0, group=c, mode="add")
    stay(world, membership(world, s0, c), date(2026, 11, 2))                      # in C since 02.11 (a stay that began the very day he left A would start a day later: rule G-8, not this test)
    pay(world, s0, c, amount=600_000)
    before_c = snap(world, s0, c)
    r = add(world, s0, group=b, mode="transfer", from_group_id=str(a.id))
    assert r["status"] == "added"
    assert snap(world, s0, c) == before_c                                        # C: the same statement, price, pauses, paid-until
    assert membership(world, s0, a).is_active is False and membership(world, s0, b).is_active is True and membership(world, s0, c).is_active is True
    (link,) = world.db.query(MembershipTransfer).filter_by(student_id=s0.id).all()
    assert link.from_membership_id == membership(world, s0, a).id and link.to_membership_id == membership(world, s0, b).id    # the one link is A -> B
    assert world.db.query(GroupAudit).filter(GroupAudit.group_id == c.id, GroupAudit.action == "student_left_group").count() == 0


def test_t1_without_a_source_a_student_of_two_groups_is_asked_which_one_and_nothing_is_written(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    add(world, s0, group=c, mode="add")
    r = add(world, s0, group=b, mode="transfer", expect=422)
    detail = r["detail"]
    assert detail["code"] == "TRANSFER_SOURCE_REQUIRED"
    assert {g["group_id"]: g["group_name"] for g in detail["groups"]} == {str(a.id): a.name, str(c.id): c.name}
    assert membership(world, s0, a).is_active and membership(world, s0, c).is_active
    assert world.db.query(MembershipTransfer).filter_by(student_id=s0.id).count() == 0
    assert world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=b.id).count() == 0


def test_t1_a_student_of_one_group_needs_no_source_and_a_source_with_mode_add_is_refused(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0, s1 = world.students[0], world.students[1]
    add(world, s0, group=b, mode="transfer")                                     # the only active group is the source
    assert membership(world, s0, a).is_active is False and membership(world, s0, b).is_active is True
    r = add(world, s1, group=b, mode="add", from_group_id=str(a.id), expect=422)
    assert r["detail"]["code"] == "SOURCE_ONLY_FOR_TRANSFER"
    r = add(world, s0, group=a, mode="transfer", from_group_id=str(a.id), expect=422)         # to the same group (s0 is in B now)
    assert r["detail"]["code"] == "TRANSFER_SOURCE_IS_TARGET"


def test_t1_the_second_administrator_who_moves_the_same_student_from_the_same_group_is_refused_clearly(world, fixed_today):
    # the student row is locked FOR UPDATE: the second request sees what the first did - A is not his active group any more
    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    add(world, s0, group=b, mode="transfer", from_group_id=str(a.id))
    r = add(world, s0, group=c, mode="transfer", from_group_id=str(a.id), expect=409)
    assert r["detail"]["code"] == "TRANSFER_SOURCE_GONE" and "Обновите страницу" in r["detail"]["message"]
    assert membership(world, s0, b).is_active is True and world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=c.id).count() == 0
    assert world.db.query(MembershipTransfer).filter_by(student_id=s0.id).count() == 1


def test_t1_a_bulk_transfer_has_the_source_of_each_student_and_skips_the_student_whose_source_is_not_clear(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    add(world, s0, group=c, mode="add")                                          # s0: A and C (no clear source); s1, s2: A only
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/bulk", params={"mode": "transfer"}, json={"student_ids": [str(s0.id), str(s1.id), str(s2.id)]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["added"] == 2 and out["skipped"] == 1
    assert out["skipped_details"][0]["student_id"] == str(s0.id) and out["skipped_details"][0]["code"] == "TRANSFER_SOURCE_REQUIRED"
    assert membership(world, s1, b).is_active and membership(world, s2, b).is_active
    assert membership(world, s0, a).is_active and membership(world, s0, c).is_active            # the refused one is untouched
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/bulk", params={"mode": "transfer"}, json={"student_ids": [str(s0.id)], "from_group_id": str(c.id)})
    assert r.json()["added"] == 1                                                # a common source named for everybody
    assert membership(world, s0, c).is_active is False and membership(world, s0, a).is_active is True


# ---------------------------------------------------------------------------------------------------- T2 the pause goes with the student


def pause(w, group, student, first, last, reason="Болезнь"):
    r = owner(w).post(f"{API}/org/memberships/{membership(w, student, group).id}/pauses", json={"first_day": first.isoformat(), "last_day": last.isoformat(), "reason": reason})
    assert r.status_code == 201, r.text
    return r.json()["pause"]["id"]


def pauses_of(w, student, group):
    w.db.expire_all()
    return sorted((p.first_day, p.last_day, p.reason, p.voided_at is not None) for p in w.db.query(MembershipPause).filter_by(membership_id=membership(w, student, group).id))


def nov_charge(w, student, group):
    """What the November period of the membership charges (the segment of the period that starts on 01.11)."""
    from tests.money1b_helpers import charge_of

    return charge_of(account(w, membership(w, student, group).id), date(2026, 11, 1))


def test_t2_the_current_and_the_future_pause_go_to_the_new_group_and_the_charge_of_the_new_group_skips_their_days(world, fixed_today):
    # Transfer on Sunday 22.11 A -> B (B: 400 000 a month, a lesson every day, November has 30 days).  A pause 20-26.11 (holds the day) and a future one 05-10.12.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    cur = pause(world, a, s0, date(2026, 11, 20), date(2026, 11, 26))
    fut = pause(world, a, s0, date(2026, 12, 5), date(2026, 12, 10), "Поездка")
    add(world, s0, group=b, mode="transfer")
    # the source keeps 20-22 (cut to the transfer day), the future one is cancelled there; the new group has 23-26 and 05-10.12 with the same reasons
    assert pauses_of(world, s0, a) == [(date(2026, 11, 20), date(2026, 11, 22), "Болезнь", False), (date(2026, 12, 5), date(2026, 12, 10), "Поездка", True)]
    assert pauses_of(world, s0, b) == [(date(2026, 11, 23), date(2026, 11, 26), "Болезнь", False), (date(2026, 12, 5), date(2026, 12, 10), "Поездка", False)]
    # B is charged from 23.11: 23..30 = 8 days, 4 of them (23-26) under the pause -> 4 days of 30: 400 000 x 4 / 30 = 53 333 -> 53 000 (a share is rounded to a thousand)
    assert nov_charge(world, s0, b) == 53_000
    # the journal names the old pause and the new one
    lines = [g for g in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action.in_(("membership_pause_set", "membership_pause_void"))) if g.snapshot.get("via") == "transfer"]
    made = {g.snapshot["moved_from_pause_id"]: g for g in lines if g.snapshot.get("moved_from_pause_id")}
    assert set(made) == {cur, fut} and all(g.group_id == b.id and g.actor_id == world.owner.id for g in made.values())
    assert {g.snapshot["pause_id"] for g in lines if not g.snapshot.get("moved_from_pause_id")} == {cur, fut}      # the cut and the cancelled in the old group: via = transfer
    # the moved pause cancelled in B gives the charge back: 8 days of 30 = 106 667 -> 107 000
    moved = world.db.query(MembershipPause).filter_by(membership_id=membership(world, s0, b).id, first_day=date(2026, 11, 23)).one()
    r = owner(world).post(f"{API}/org/pauses/{moved.id}/void", json={"reason": "Выздоровел"})
    assert r.status_code == 200, r.text
    assert nov_charge(world, s0, b) == 107_000


def test_t2_a_leave_keeps_the_old_rule_the_pauses_are_cut_and_nothing_is_created(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    pause(world, a, s0, date(2026, 11, 20), date(2026, 11, 26))
    r = owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={})
    assert r.status_code == 200, r.text
    assert pauses_of(world, s0, a) == [(date(2026, 11, 20), date(2026, 11, 22), "Болезнь", False)]
    assert world.db.query(MembershipPause).filter_by(student_id=s0.id).count() == 1


def test_t2_a_package_centre_moves_the_pause_too_the_dates_of_his_absence_travel_whatever_the_charge(world, fixed_today):
    # in a package centre the pause does not shorten a charge (the package is spent by held lessons) but it dates the absence: the unspent days and the paid-until estimate - so it moves as well
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    settings_block(world, period_mode="lessons", lessons_per_payment=12)
    s0 = world.students[0]
    pause(world, a, s0, date(2026, 11, 22), date(2026, 11, 28))
    add(world, s0, group=b, mode="transfer")
    assert pauses_of(world, s0, b) == [(date(2026, 11, 23), date(2026, 11, 28), "Болезнь", False)]


# ---------------------------------------------------------------------------------------------------- T3 what ends, what moves, what stays


def joined(w, student, group, day):
    stay(w, membership(w, student, group), day)


def codes(items):
    return [i["code"] for i in items]


def test_t3_a_debt_that_stays_in_the_old_group_is_named_with_its_sum_and_the_false_phrase_is_not_said(world, fixed_today):
    # A (300 000 a month, 30 lessons of November), the student is in A since 02.11 and has paid nothing; the transfer is on 22.11.  Days 02..22 = 21 days = 21 lessons: 300 000 x 21 / 30 = 210 000 of debt.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    dry = add(world, s0, group=b, mode="transfer", dry_run=True)["effects"]
    real = add(world, s0, group=b, mode="transfer")["effects"]
    assert dry == real                                                           # the window says what the server writes (И4)
    (debt,) = real["stays"]
    assert debt["code"] == "debt" and debt["amount"] == 210_000 and debt["group_id"] == str(a.id) and debt["group_name"] == a.name
    assert debt["text"] == f"Долг 210 000 сум останется за учеником в группе «{a.name}»: принять или простить его можно в её выписке"
    assert "нет оплат" not in (real["money_text"] or "") and real["money_text"] is None and codes(real["moves"]) == []


def test_t3_nothing_to_carry_says_it_only_when_it_is_true(world, fixed_today):
    # the student joined A on 22.11, the day of the transfer: A holds the day (a lesson), nothing paid, nothing owed beyond the day itself - so use a student with an advance of exactly zero and no debt: paid 210 000 for the 21 days
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=210_000)                                            # exactly what 02..22.11 costs (300 000 x 21 / 30): no advance, no debt
    out = add(world, s0, group=b, mode="transfer")["effects"]
    assert out["stays"] == [] and out["moves"] == [] and out["money_text"] == "Деньги не изменятся: в прошлой группе нет аванса и долга"


def test_t3_an_advance_moves_with_the_sum_and_the_individual_price_and_a_scheduled_price_end(world, fixed_today):
    # paid 600 000 into A, 210 000 spent by 22.11 -> an advance of 390 000 travels; the individual price 250 000 of A and a price scheduled from 01.12 (200 000) end
    from tests.pg_world import give_price

    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    m = membership(world, s0, a)
    give_price(world.db, m, 300_000, reason="договорённость")                  # an individual price equal to the group's: the spent days stay 210 000
    world.db.add(MembershipPriceOverride(membership_id=m.id, amount=200_000, valid_from=date(2026, 12, 1), reason="акция", created_by=world.owner.id, created_at=datetime(2026, 11, 10)))
    world.db.commit()
    pay(world, s0, a, amount=600_000)
    out = add(world, s0, group=b, mode="transfer")["effects"]
    (money,) = [i for i in out["moves"] if i["code"] == "money"]
    assert money["amount"] == 390_000 and "390 000" in money["text"] and money["group_name"] == a.name
    ends = {i["code"]: i for i in out["ends"]}
    assert set(ends) == {"individual_price", "scheduled_price"}
    assert ends["individual_price"]["amount"] == 300_000 and ends["scheduled_price"]["amount"] == 200_000 and ends["scheduled_price"]["valid_from"] == "2026-12-01"
    assert "01.12.2026" in ends["scheduled_price"]["text"] and "будет отменена" in ends["scheduled_price"]["text"]
    assert out["texts"] == [i["text"] for i in (*out["moves"], *out["stays"], *out["ends"])]


def test_t3_package_centre_says_lessons_at_the_price_of_the_new_group_and_the_rest(world, fixed_today):
    from src.auth.leave_effects import package_split

    # B costs 400 000 for 12 lessons.  390 000: 11 lessons cost round_1000(400 000 x 11 / 12 = 366 667) = 367 000, the 12th would reach 400 000 -> 11 lessons, the rest 390 000 - 367 000 = 23 000
    assert package_split(390_000, 400_000, 12) == (11, 23_000)
    # 600 000 = one whole package (400 000) + 200 000: 6 lessons cost 200 000 exactly (400 000 x 6 / 12), the 7th 233 000 -> 12 + 6 = 18 lessons, the rest 0
    assert package_split(600_000, 400_000, 12) == (18, 0)
    assert package_split(1_000, 400_000, 12) == (0, 1_000) and package_split(5, None, 12) is None
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    settings_block(world, period_mode="lessons", lessons_per_payment=12)
    s0 = world.students[0]
    pay(world, s0, a, amount=600_000)                                            # no lesson is held: nothing is spent, the whole 600 000 travels
    out = add(world, s0, group=b, mode="transfer")["effects"]
    (money,) = [i for i in out["moves"] if i["code"] == "money"]
    assert money["amount"] == 600_000 and (money["lessons"], money["rest"]) == (18, 0)
    assert money["text"] == f"Перейдёт 600 000 сум в группу «{b.name}»: 18 уроков по цене новой группы и остаток 0 сум".replace(a.name, b.name)


def test_t3_a_leave_and_an_archiving_say_what_ends_and_what_stays_and_the_answer_equals_the_window(world, fixed_today):
    from tests.pg_world import give_price

    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    give_price(world.db, membership(world, s0, a), 250_000, reason="договорённость")
    pause(world, a, s0, date(2026, 11, 25), date(2026, 11, 30))
    pay(world, s0, a, amount=400_000)                                            # 21 days at 250 000 a month = 175 000 -> an advance of 225 000 that stays unless it is returned
    client = owner(world)
    window = client.get(f"{API}/org/groups/{a.id}/students/{s0.id}/leave-preview").json()["memberships"][0]["effects"]
    assert window["kind"] == "leave" and codes(window["stays"]) == ["advance"] and window["stays"][0]["amount"] == 225_000
    assert sorted(codes(window["ends"])) == ["individual_price", "pause"]
    done = client.post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).json()["effects"]
    assert done == window
    # the same student archived (a second world student): the archive window and the answer
    s1 = world.students[1]
    joined(world, s1, a, date(2026, 11, 2))
    win = client.get(f"{API}/org/students/{s1.id}/archive-preview").json()["memberships"][0]["effects"]
    ans = client.post(f"{API}/org/students/{s1.id}/archive", json={}).json()["effects"]
    assert win["kind"] == "archive" and ans == [win] and codes(win["stays"]) == ["debt"] and win["stays"][0]["amount"] == 210_000


def test_t3_without_the_right_to_money_the_facts_come_without_sums(world, fixed_today):
    from tests.pg_world import make_client

    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))                                      # owes 210 000
    admin = make_client(world, user=world.admin, org_role="admin")
    effects = admin.get(f"{API}/org/groups/{a.id}/students/{s0.id}/leave-preview").json()["memberships"][0]["effects"]
    (debt,) = effects["stays"]
    assert debt["code"] == "debt" and "amount" not in debt and not any(ch.isdigit() for ch in debt["text"])


# ---------------------------------------------------------------------------------------------------- T4 an «Оплата с» that has not begun goes with the student


def test_t4_a_payment_start_in_the_future_goes_to_the_new_group_and_it_charges_from_the_same_day(world, fixed_today):
    from tests.money1b_helpers import fresh_student
    from src.models.auth import StudentGroupPeriod

    # a new student is put into A on 22.11 with «Оплата с 25.11» and moved to B (400 000) the same day.  B charges from 25.11, not from the day after the transfer (23.11):
    # 25..30.11 = 6 lessons of 30: 400 000 x 6 / 30 = 80 000.  Without the rule B would start on 23.11: 8 lessons = 400 000 x 8 / 30 = 106 667 -> 107 000.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s = fresh_student(world)
    add(world, s, group=a, mode="add", pay_from="2026-11-25")
    out = add(world, s, group=b, mode="transfer")
    assert nov_charge(world, s, b) == 80_000
    world.db.expire_all()
    (period,) = world.db.query(StudentGroupPeriod).filter_by(membership_id=membership(world, s, b).id).all()
    assert period.billing_from == date(2026, 11, 25)
    (moved,) = [m for m in out["effects"]["moves"] if m["code"] == "billing_from"]
    assert moved["date"] == "2026-11-25" and "25.11.2026" in moved["text"] and moved["group_name"] == a.name
    # the old group charges nothing (its stay ended before its first charged day)
    assert account(world, membership(world, s, a).id).charged == 0


def test_t4_a_payment_start_that_has_begun_does_not_move(world, fixed_today):
    # «Оплата с» of 02.11 is in the past on 22.11: B charges from the day after the transfer as always: 23..30 = 8 lessons -> 107 000
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    out = add(world, s0, group=b, mode="transfer")
    assert nov_charge(world, s0, b) == 107_000
    assert [m for m in out["effects"]["moves"] if m["code"] == "billing_from"] == []


# ---------------------------------------------------------------------------------------------------- T5 «числа устарели» at a transfer


def pid(created):
    return created.json()["payment"]["id"] if hasattr(created, "json") else created


def test_t5_a_payment_cancelled_between_the_window_and_the_transfer_is_409_and_nothing_is_written(world, fixed_today):
    # in A since 02.11 (21 days to 22.11 = 210 000 spent of 300 000 a month); paid 400 000 + 200 000.  The window shows 600 000 - 210 000 = 390 000.  The 200 000 payment is cancelled meanwhile: 400 000 - 210 000 = 190 000.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=400_000)
    second = pay(world, s0, a, amount=200_000, paid_on=date(2026, 11, 21))
    window = add(world, s0, group=b, mode="transfer", dry_run=True)
    (money,) = [m for m in window["effects"]["moves"] if m["code"] == "money"]
    assert money["amount"] == 390_000
    assert owner(world).post(f"{API}/org/payments/{second['payment']['id']}/void", json={"reason": "Внесли дважды"}).status_code == 200
    r = add(world, s0, group=b, mode="transfer", expected_carried=390_000, expect=409)
    assert r["detail"]["code"] == "BALANCE_CHANGED" and r["detail"]["carried"] == 190_000 and r["detail"]["fresh"] == [{"carried": 190_000}]
    assert membership(world, s0, a).is_active and world.db.query(MembershipTransfer).filter_by(student_id=s0.id).count() == 0     # nothing written
    assert world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=b.id).count() == 0
    again = add(world, s0, group=b, mode="transfer", expected_carried=190_000)                 # the fresh number goes through
    assert again["status"] == "added"


def test_t5_a_transfer_without_expected_carried_is_not_checked_and_a_matching_one_passes(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0, s1 = world.students[0], world.students[1]
    joined(world, s0, a, date(2026, 11, 2))
    joined(world, s1, a, date(2026, 11, 2))
    pay(world, s0, a, amount=600_000)
    pay(world, s1, a, amount=600_000)
    assert add(world, s0, group=b, mode="transfer")["status"] == "added"
    assert add(world, s1, group=b, mode="transfer", expected_carried=390_000)["status"] == "added"


def test_one_closing_for_leave_transfer_and_archive_each_says_how_the_stay_ended_in_the_journal_of_its_pauses(world, fixed_today):
    # three students, each with a pause 22-26.11 in A (today 20.11 is Friday): a leave, a transfer and an archiving all go through ``close_stay``; the pause starts after the 20th, is cancelled and the line says via
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        pause(world, a, s, date(2026, 11, 22), date(2026, 11, 26))
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).status_code == 200
    add(world, s1, group=b, mode="transfer")
    assert owner(world).post(f"{API}/org/students/{s2.id}/archive", json={}).status_code == 200
    vias = {g.snapshot["student_id"]: g.snapshot["via"] for g in world.db.query(GroupAudit).filter(
        GroupAudit.org_id == world.org.id, GroupAudit.group_id == a.id, GroupAudit.action.in_(("membership_pause_set", "membership_pause_void"))) if "via" in g.snapshot}
    assert vias == {str(s0.id): "leave", str(s1.id): "transfer", str(s2.id): "archive"}


# ---------------------------------------------------------------------------------------------------- T6 a return on the day of leaving cancels the leaving


def rich_stay(world, a):
    """A student of A since 02.11 with everything a leaving takes away: an individual price 240 000 (the group's is 300 000), a price scheduled from 01.12 (200 000), the pause 22-24.11 that holds the
    day of leaving (Sunday 22.11) and a future pause 25-28.11.  Returns the student."""
    from tests.pg_world import give_price

    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    m = membership(world, s0, a)
    give_price(world.db, m, 240_000, reason="договорённость")
    world.db.add(MembershipPriceOverride(membership_id=m.id, amount=200_000, valid_from=date(2026, 12, 1), reason="акция", created_by=world.owner.id, created_at=datetime(2026, 11, 10)))
    world.db.commit()
    pause(world, a, s0, date(2026, 11, 22), date(2026, 11, 24))
    pause(world, a, s0, date(2026, 11, 25), date(2026, 11, 28), "Поездка")
    return s0


def test_t6_a_return_on_the_day_of_leaving_is_a_cancel_of_the_leaving(world, fixed_today):
    # November of the student in A: 02..30 = 29 days, 7 of them under pauses (22-24 and 25-28) = 22 days; the individual price 240 000: 240 000 x 22 / 30 = 176 000.
    # Left on the 22nd: the pause 22-24 is cut to 22, the 25-28 one is cancelled, the price ends on the 23rd, the scheduled one is cancelled: 02..21 = 20 days = 240 000 x 20 / 30 = 160 000.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = rich_stay(world, a)
    pay(world, s0, a, amount=100_000)
    before = snap(world, s0, a)
    assert nov_charge(world, s0, a) == 176_000
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).status_code == 200
    assert nov_charge(world, s0, a) == 160_000 and snap(world, s0, a) != before
    out = add(world, s0, group=a, mode="add")                                    # back the same day: as if he had not left
    assert out["leave_undo"]["undone"] is True and {i["code"] for i in out["leave_undo"]["restored"]} == {"individual_price", "scheduled_price", "pause"}
    assert snap(world, s0, a) == before                                          # the statement, the prices (the scheduled one too), the pauses, «оплачено до»: the same as before
    assert nov_charge(world, s0, a) == 176_000
    (line,) = world.db.query(GroupAudit).filter(GroupAudit.group_id == a.id, GroupAudit.action == "student_left_group", GroupAudit.snapshot["undone"].astext == "true").all()
    assert line.snapshot["membership_id"] == str(membership(world, s0, a).id) and line.actor_id == world.owner.id and len(line.snapshot["restored"]) == 4      # 2 prices + 2 pauses, each with its id


def test_t6_the_next_day_the_return_is_the_ordinary_one_and_nothing_is_restored(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = rich_stay(world, a)
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).status_code == 200
    fixed_today(date(2026, 11, 23))
    out = add(world, s0, group=a, mode="add")
    assert out.get("leave_undo") is None or out["leave_undo"]["undone"] is False
    assert [(p[0], p[1], p[3]) for p in pauses_of(world, s0, a)] == [(date(2026, 11, 22), date(2026, 11, 22), False), (date(2026, 11, 25), date(2026, 11, 28), True)]      # the cut pause stays cut, the cancelled stays cancelled
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=membership(world, s0, a).id, valid_from=date(2026, 12, 1)).count() == 0     # the scheduled price is gone


def test_t6_a_refund_made_after_the_leaving_forbids_the_cancel_and_the_window_says_why(world, fixed_today):
    # paid 400 000, the leaving on 22.11 settles with a refund of the advance; the return the same day is an ordinary one (the returned money stays returned)
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = rich_stay(world, a)
    pay(world, s0, a, amount=400_000)
    r = owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={"settle": [{"membership_id": str(membership(world, s0, a).id), "action": "refund", "method": "cash"}]})
    assert r.status_code == 200, r.text
    dry = add(world, s0, group=a, mode="add", dry_run=True)
    assert dry["leave_undo"]["undone"] is False and "возврат" in dry["leave_undo"]["text"].lower()
    real = add(world, s0, group=a, mode="add")
    assert real["leave_undo"] == dry["leave_undo"] and pauses_of(world, s0, a)[1][3] is True            # nothing restored


def test_t6_a_forgiveness_made_with_the_leaving_forbids_the_cancel_as_well_and_the_window_says_why(world, fixed_today):
    # in A since 02.11 at 300 000 a month, left on 22.11 owing 21 days x 10 000 = 210 000, the debt is forgiven by the leaving; the same-day return is an ordinary one: the forgiveness stays a forgiveness
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    r = owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={"settle": [{"membership_id": str(membership(world, s0, a).id), "action": "writeoff", "expected_debt": 210_000, "reason": "договорённость"}]})
    assert r.status_code == 200, r.text
    dry = add(world, s0, group=a, mode="add", dry_run=True)
    assert dry["leave_undo"]["undone"] is False and "прощён долг" in dry["leave_undo"]["text"].lower()
    real = add(world, s0, group=a, mode="add")
    assert real["leave_undo"] == dry["leave_undo"]


def test_t6_a_transfer_back_on_the_same_day_gives_every_membership_its_numbers_back(world, fixed_today):
    # A -> B -> A on 22.11.  Paid 600 000 into A: 02..30 under pauses = 22 days at 240 000 = 176 000 charged for November; the transfer takes the advance of the 22nd (600 000 - 160 000 = 440 000) to B
    # and the transfer back brings it home: A is exactly what it was, B charged nothing (its stay began and ended on the same day: rule G-8)
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = rich_stay(world, a)
    pay(world, s0, a, amount=600_000)
    before = snap(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    assert snap(world, s0, a) != before
    out = add(world, s0, group=a, mode="transfer")
    assert out["leave_undo"]["undone"] is True
    assert snap(world, s0, a) == before
    mb = membership(world, s0, b)
    assert mb.is_active is False and account(world, mb.id).charged == 0 and account(world, mb.id).advance == 0 and account(world, mb.id).debt == 0
    assert pauses_of(world, s0, b) == [(date(2026, 11, 23), date(2026, 11, 24), "Болезнь", True), (date(2026, 11, 25), date(2026, 11, 28), "Поездка", True)]      # what went to B is cancelled there


def test_t6_a_transfer_back_the_next_day_is_an_ordinary_one(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = rich_stay(world, a)
    add(world, s0, group=b, mode="transfer")
    fixed_today(date(2026, 11, 23))
    out = add(world, s0, group=a, mode="transfer")
    assert out.get("leave_undo") is None or out["leave_undo"]["undone"] is False
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=membership(world, s0, a).id, valid_from=date(2026, 12, 1)).count() == 0


# ---------------------------------------------------------------------------------------------------- T7 «Восстановить оплату»


def void(w, payment, reason="Внесли по ошибке", client=None, expect=200, dry_run=False):
    r = (client or owner(w)).post(f"{API}/org/payments/{payment['payment']['id']}/void", params={"dry_run": "true"} if dry_run else {}, json={"reason": reason})
    assert r.status_code == expect, r.text
    return r.json()


def restore(w, payment, reason="Отменили по ошибке", client=None, expect=201, dry_run=False):
    r = (client or owner(w)).post(f"{API}/org/payments/{payment['payment']['id']}/restore", params={"dry_run": "true"} if dry_run else {}, json={"reason": reason})
    assert r.status_code == expect, r.text
    return r.json()


def test_t7_a_student_who_left_with_an_advance_gets_every_number_back(world, fixed_today):
    # in A since 02.11 at 300 000 a month, left on 22.11: 02..22 = 21 days = 210 000 charged.  Paid 600 000: an advance of 390 000 stays in A.
    # Cancelled: nothing paid, a debt of 210 000.  The old rule allowed a new payment of 210 000 only (the ceiling of a student who left); the restoration puts the 600 000 back: 390 000 again.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=600_000)
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s0.id}/leave", json={}).status_code == 200
    before = snap(world, s0, a)
    assert account(world, membership(world, s0, a).id).advance == 390_000
    void(world, p)
    assert account(world, membership(world, s0, a).id).debt == 210_000
    again = pay(world, s0, a, amount=600_000, expect=422)                        # a NEW payment is refused: not more than the debt
    assert again["detail"]["code"] == "PAYMENT_EXCEEDS_DEBT"
    window = restore(world, p, dry_run=True)
    assert window["dry_run"] is True and window["payment"]["amount"] == 600_000
    out = restore(world, p)
    new = out["payment"]
    assert new["amount"] == 600_000 and new["paid_on"] == p["payment"]["paid_on"] and new["method"] == p["payment"]["method"]
    assert new["replaces_payment_id"] == p["payment"]["id"] and new["restored_from_receipt_no"] == p["payment"]["receipt_no"] and new["receipt_no"] != p["payment"]["receipt_no"]
    assert snap(world, s0, a) == before                                          # the statement: «оплачено», advance 390 000, debt 0, paid-until - all as before the cancellation
    line = world.db.query(GroupAudit).filter(GroupAudit.group_id == a.id, GroupAudit.action == "payment_replace", GroupAudit.snapshot["event"].astext == "payment_restored").one()
    assert line.snapshot["payment_id"] == p["payment"]["id"] and line.snapshot["new_payment_id"] == new["id"] and line.snapshot["reason"] == "Отменили по ошибке" and line.actor_id == world.owner.id


def test_t7_a_restoration_is_once_only_needs_a_cancelled_payment_and_a_reason(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=100_000)
    r = restore(world, p, expect=409)                                            # not cancelled
    assert r["detail"]["code"] == "PAYMENT_NOT_VOIDED"
    void(world, p)
    r = restore(world, p, reason="  ", expect=422)
    restore(world, p)
    r = restore(world, p, expect=409)                                            # the second time
    assert r["detail"]["code"] == "PAYMENT_ALREADY_RESTORED" and "уже заменена оплатой" in r["detail"]["message"]
    q = pay(world, s0, a, amount=50_000)                                         # a payment that was CORRECTED (the old one is cancelled by the correction) is not restored either
    fixed = owner(world).post(f"{API}/org/payments/{q['payment']['id']}/replace", json={"amount": 60_000, "paid_on": q["payment"]["paid_on"], "method": "cash", "reason": "Опечатка"})
    assert fixed.status_code == 201, fixed.text
    assert restore(world, q, expect=409)["detail"]["code"] == "PAYMENT_ALREADY_RESTORED"


def test_t7_a_transferred_student_restoring_in_the_old_group_brings_the_transfer_back(world, fixed_today):
    # paid 600 000 into A; the transfer on 22.11 takes 600 000 - 210 000 = 390 000 to B.  Cancelling the payment of A leaves B without money, the restoration gives B the 390 000 again.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=600_000)
    add(world, s0, group=b, mode="transfer")
    before = (snap(world, s0, a), snap(world, s0, b))
    voided = void(world, p)
    assert [d["amount"] for d in voided["advance_drops"]] == [390_000] and snap(world, s0, b) != before[1]        # B loses the whole advance that came from A
    window = restore(world, p, dry_run=True)
    # the window (the same numbers as the write) names the OTHER group: B had no money after the cancellation, the restoration gives it the 390 000 back
    assert [(g["group_name"], g["advance_grew"], g["advance"], g["debt_fell"]) for g in window["other_gains"]] == [("Бета", 390_000, 390_000, 0)]
    assert f"В группе «Бета» аванс вырастет на {tx.money(390_000)}" in window["money_effects"]["text"]
    assert snap(world, s0, b) != before[1]                                       # a dry run writes nothing
    restore(world, p)
    assert (snap(world, s0, a), snap(world, s0, b)) == before
    assert account(world, membership(world, s0, b).id).transferred_in == 390_000


def test_t7_an_active_student_and_the_rights_and_the_other_centre(world, fixed_today):
    from tests.pg_world import make_client

    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=200_000)
    before = snap(world, s0, a)
    void(world, p)
    teacher = make_client(world, user=world.teacher, role="admin", org_role="teacher")
    assert teacher.post(f"{API}/org/payments/{p['payment']['id']}/restore", json={"reason": "нет права"}).status_code == 403
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="owner", role="owner")
    world.db.query(OrganizationMember).filter_by(user_id=world.foreign_admin.id).update({"org_role": "owner"})
    world.db.commit()
    assert foreign.post(f"{API}/org/payments/{p['payment']['id']}/restore", json={"reason": "чужая"}).status_code == 404
    assert world.db.query(StudentPayment).filter_by(membership_id=membership(world, s0, a).id, voided_at=None).count() == 0       # nothing restored by them
    restore(world, p)
    assert snap(world, s0, a) == before


def test_t7_the_till_takes_a_restoration_as_a_correction_by_the_same_sum(world, fixed_today):
    # cash 100 000 on 18.11; cancelled on 19.11, restored on 20.11 (the same rules as a correction, Д-7): 18.11 - the day the payment is of - stays as it was (a closed day never moves);
    # 19.11: the till gave 100 000 back («отменено»); 20.11: the till takes 100 000 again («принято»).  A second payment (70 000, of 19.11) cancelled AND restored on 20.11 itself moves nothing on 20.11 (a correction by the same sum).
    a, b, c = groups3(world, fixed_today, date(2026, 11, 18))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))

    def till(day):
        r = owner(world).get(f"{API}/org/payments/income", params={"from": day, "to": day})
        assert r.status_code == 200, r.text
        cash = r.json()["cash"]
        return cash["cash_received"], cash["cash_cancelled"], cash["cash_corrected"]

    p = pay(world, s0, a, amount=100_000, method="cash")
    assert till("2026-11-18") == (100_000, 0, 0)
    fixed_today(date(2026, 11, 19))
    q = pay(world, s0, a, amount=70_000, method="cash")
    void(world, p)
    assert till("2026-11-19") == (70_000, 100_000, 0) and till("2026-11-18") == (100_000, 0, 0)
    fixed_today(date(2026, 11, 20))
    restore(world, p)
    assert till("2026-11-20") == (100_000, 0, 0) and till("2026-11-19") == (70_000, 100_000, 0)
    void(world, q)
    restore(world, q)
    assert till("2026-11-20") == (100_000, 0, 0)                                  # 70 000 cancelled and restored the same day: nothing moves


def test_t7_the_journal_of_decisions_names_the_restoration_apart_from_a_correction_and_a_carried_payment_is_not_restored(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=100_000)
    q = pay(world, s0, a, amount=50_000)
    void(world, p)
    new = restore(world, p)["payment"]
    fixed = owner(world).post(f"{API}/org/payments/{q['payment']['id']}/replace", json={"amount": 60_000, "paid_on": q["payment"]["paid_on"], "method": "cash", "reason": "Опечатка"})
    assert fixed.status_code == 201, fixed.text

    def journal(kind=None):
        r = owner(world).get(f"{API}/org/payments/decisions", params={"from": "2026-11-22", "to": "2026-11-22", **({"kind": kind} if kind else {})})
        assert r.status_code == 200, r.text
        return r.json()["rows"]

    restored = journal("payment_restored")
    assert len(restored) == 1 and restored[0]["kind_label"] == "Оплата восстановлена" and restored[0]["reason"] == "Отменили по ошибке" and restored[0]["amount"] == 100_000
    assert f"№ {p['payment']['receipt_no']}" in restored[0]["text"] and f"№ {new['receipt_no']}" in restored[0]["text"] and tx.money(100_000) in restored[0]["text"]
    assert [r["kind"] for r in journal("payment_replace")] == ["payment_replace"]            # the correction of the other payment: not mixed with the restoration
    assert sorted(r["kind"] for r in journal()) == sorted(["payment_void", "payment_restored", "payment_replace"])      # the cancellation of p, its restoration, the correction of q: each named once
    # a payment that came from another group (a legacy carry-over row) is not restored: its original is
    m = membership(world, s0, a)
    carry = StudentPayment(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, amount=0, paid_on=date(2026, 11, 22), valid_from=date(2026, 11, 22),
                           valid_to=date(2026, 11, 22), carried_from_payment_id=p["payment"]["id"], kind="legacy_carry", created_by=world.owner.id, voided_at=datetime(2026, 11, 22, 8), voided_by=world.owner.id, void_reason="x")
    world.db.add(carry)
    world.db.commit()
    r = owner(world).post(f"{API}/org/payments/{carry.id}/restore", json={"reason": "по ошибке"})
    assert r.status_code == 409 and "перешедшие из другой группы" in str(r.json()).lower()


def test_t7_an_admin_without_the_right_is_refused_a_dry_run_writes_nothing_and_the_cheque_goes_with_the_restoration(world, fixed_today):
    from tests.pg_world import make_client

    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p = pay(world, s0, a, amount=100_000)
    world.db.add(StudentPaymentFile(org_id=world.org.id, payment_id=p["payment"]["id"], filename="cheque.png", content_type="image/png", size_bytes=3, data=b"abc", uploaded_by=world.owner.id, uploaded_at=datetime(2026, 11, 22, 8)))
    world.db.commit()
    void(world, p)
    admin = make_client(world, user=world.admin, org_role="admin")                # an administrator without the right to money
    for params in ({}, {"dry_run": "true"}):
        assert admin.post(f"{API}/org/payments/{p['payment']['id']}/restore", params=params, json={"reason": "Отменили по ошибке"}).status_code == 403
    payments_before, lines_before = world.db.query(StudentPayment).count(), world.db.query(GroupAudit).count()
    window = restore(world, p, dry_run=True)
    assert window["dry_run"] is True and "money_effects" in window                # «Что изменится»: the same block as the cancellation has
    world.db.expire_all()
    assert (world.db.query(StudentPayment).count(), world.db.query(GroupAudit).count()) == (payments_before, lines_before)
    new = restore(world, p)["payment"]
    # the cheque (3 bytes) is copied to the new payment; the cancelled one keeps its own as the trace
    assert [(f.filename, f.size_bytes) for f in world.db.query(StudentPaymentFile).filter_by(payment_id=new["id"])] == [("cheque.png", 3)]
    assert world.db.query(StudentPaymentFile).filter_by(payment_id=p["payment"]["id"]).count() == 1


def test_t7_the_list_of_payments_says_which_cancelled_payment_can_be_restored_and_by_what_it_was_replaced(world, fixed_today):
    # three payments of A: p cancelled (restorable), q cancelled and restored (no more: replaced by the restoration), r alive (nothing to restore); the list is what the button of the statement reads
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    p, q, r = (pay(world, s0, a, amount=n) for n in (100_000, 50_000, 20_000))
    void(world, p)
    void(world, q)
    new_q = restore(world, q)["payment"]

    def listed():
        rows = owner(world).get(f"{API}/org/memberships/{membership(world, s0, a).id}/payments").json()
        return {row["id"]: (row["restorable"], row["replaced_by_receipt_no"]) for row in rows}

    got = listed()
    assert got[p["payment"]["id"]] == (True, None)
    assert got[q["payment"]["id"]] == (False, new_q["receipt_no"])
    assert got[new_q["id"]] == (False, None) and got[r["payment"]["id"]] == (False, None)
    void(world, {"payment": new_q})                                              # the restored payment is cancelled again: it can be restored (the first one stays replaced by it)
    got = listed()
    assert got[new_q["id"]] == (True, None) and got[q["payment"]["id"]][0] is False


# ---------------------------------------------------------------------------------------------------- T8 a refusal of a refund names the group where the advance went


def test_t8_a_refund_in_the_old_group_names_the_group_where_the_advance_went_and_a_plain_empty_account_keeps_its_text(world, fixed_today):
    # paid 600 000 into A (02.11 at 300 000 a month); the transfer on 22.11 takes 600 000 - 21 x 10 000 = 390 000 to B: A holds no advance any more, B holds 390 000
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=600_000)
    add(world, s0, group=b, mode="transfer")
    body = {"amount": 100_000, "method": "cash", "reason": "Возврат по просьбе"}
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s0, a).id}/refunds", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "REFUND_EXCEEDS_ADVANCE" and r.json()["detail"]["message"] == "Аванс ученика перешёл в группу «Бета» — возврат оформите там"
    # the group that holds the money refunds as usual
    assert owner(world).post(f"{API}/org/memberships/{membership(world, s0, b).id}/refunds", json=body).status_code == 201
    # a student who simply has no advance (no transfer): the old text
    s1 = world.students[1]
    add(world, s1, group=c, mode="add")
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s1, c).id}/refunds", json=body)
    assert r.status_code == 422 and r.json()["detail"]["message"] == "Возвращать нечего: аванса нет"
