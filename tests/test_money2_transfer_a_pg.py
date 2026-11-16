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
