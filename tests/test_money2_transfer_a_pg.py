"""Money-2 / wave A (transfer, leaving, return - the server): T1..T9 (PostgreSQL).  Every money number is worked out by hand in the comment of its test."""
from datetime import date, datetime, timedelta

import pytest

from src.models.auth import (
    GroupAudit, MembershipPause, MembershipPriceOverride, MembershipTransfer, StudentGroup,
)

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
    """Everything about one membership that a transfer of ANOTHER group must not move: the ledger, the stay, the pauses, the prices."""
    m = membership(w, student, group)
    led = account(w, m.id)
    pauses = sorted((p.first_day, p.last_day, p.voided_at is None) for p in w.db.query(MembershipPause).filter_by(membership_id=m.id))
    prices = sorted((r.valid_from, r.amount, r.reason) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id))
    return (m.is_active, m.left_at, led.charged, led.advance, led.debt, led.covered_through, led.transferred_in, tuple(pauses), tuple(prices))


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
