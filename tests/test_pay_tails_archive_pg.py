"""PAY-TAILS part 1 (А3 + А6.8): the charge of a CLOSED stay does not depend on the fate of the group (archive / restore), PostgreSQL, through the routes of the product.

World: November 2026, group "Арх" - Tue / Thu / Fri (3, 5, 6, 10, 12, 13, 17, 19, 20, 24, 26, 27 = 12 lessons), «Общий месяц» from the 1st, price 600 000 = 50 000 a lesson.
Alia joined 02.11 and left 11.11 after four lessons (3, 5, 6, 10): 600 000 x 4 / 12 = 200 000 - ALWAYS, whatever happens to the group afterwards (before the fix: 400 000 after
the archive of 14.11 - the denominator 12 -> 6 - and 218 000 after the restore of 18.11 - denominator 11)."""
from datetime import date, datetime

import pytest

from src.models.auth import StudentGroup

from sqlalchemy.orm.attributes import flag_modified

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

TTF = [slot("s2", 2, "2026-01-01", "14:00", "15:00"), slot("s4", 4, "2026-01-01", "14:00", "15:00"), slot("s5", 5, "2026-01-01", "14:00", "15:00")]      # Tue, Thu, Fri
LEFT = date(2026, 11, 11)


def tue_world(w, fixed_today, price=600_000):
    fixed_today(date(2026, 11, 2))
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    group = make_group(w.db, w.org, w.teacher, "Арх")
    give_group_price(w.db, group, price)
    set_schedule(w, TTF, group)
    ensure_billing(w, group, price=None)
    return group


def archive(w, group, expect=200):
    r = owner(w).post(f"{API}/org/groups/{group.id}/archive", json={"missedLessonsAction": "cancel"})
    assert r.status_code == expect, r.text
    return r


def restore(w, group):
    r = owner(w).post(f"{API}/org/groups/{group.id}/restore", json={})
    assert r.status_code == 200, r.text


def leave(w, group, student):
    r = owner(w).post(f"{API}/org/groups/{group.id}/students/{student.id}/leave", json={})
    assert r.status_code == 200, r.text


def facts(w, student, group, today):
    m = membership(w, student, group)
    led = account(w, m.id, today)
    return led.charged, led.debt, led.advance


def alia_left(w, fixed_today, *, paid=200_000):
    """Alia in «Арх» 02.11 - 11.11 (four lessons), paid ``paid`` on the day of leaving."""
    group = tue_world(w, fixed_today)
    s = fresh_student(w, "Алия")
    add(w, s, group=group, mode="add")
    fixed_today(LEFT)
    leave(w, group, s)
    if paid:
        pay(w, s, group, amount=paid, paid_on=LEFT)
    return group, s


def test_g1_the_charge_of_a_leaver_is_200_000_before_and_after_every_archive_and_restore(world, fixed_today):
    # 600 000 x 4 / 12 = 200 000; paid 200 000 -> debt 0, advance 0 in all four points (the denominator is 12 in all of them)
    group, s = alia_left(world, fixed_today)
    assert facts(world, s, group, LEFT) == (200_000, 0, 0)
    fixed_today(date(2026, 11, 14))
    archive(world, group)
    assert facts(world, s, group, date(2026, 11, 14)) == (200_000, 0, 0)          # was 400 000 / debt 200 000
    fixed_today(date(2026, 11, 18))
    restore(world, group)
    assert facts(world, s, group, date(2026, 11, 18)) == (200_000, 0, 0)          # was 218 000 / debt 18 000
    fixed_today(date(2026, 11, 20))
    archive(world, group)
    assert facts(world, s, group, date(2026, 11, 20)) == (200_000, 0, 0)          # the second archive: still the same
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s, group).id}/statement")
    assert r.status_code == 200 and r.json()["summary"]["charged"] == 200_000 and r.json()["summary"]["reconciles"] is True


def test_g2_a_student_enrolled_after_the_restore_pays_his_lessons_of_the_full_month(world, fixed_today):
    # the group was archived 14.11 and restored 18.11; Boris is enrolled 19.11 (Thu): lessons 19, 20, 24, 26, 27 = 5 of 12 -> 600 000 x 5 / 12 = 250 000
    group, s = alia_left(world, fixed_today)
    fixed_today(date(2026, 11, 14))
    archive(world, group)
    fixed_today(date(2026, 11, 18))
    restore(world, group)
    fixed_today(date(2026, 11, 19))
    b = fresh_student(world, "Борис")
    add(world, b, group=group, mode="add")
    led = account(world, membership(world, b, group).id, date(2026, 11, 19))
    (seg,) = [x for x in led.segments if x.kind == "period"]
    assert (seg.amount, seg.lessons_in_period) == (250_000, 12)


def test_g3_a_leaver_with_a_debt_blocks_the_archive_and_the_charge_does_not_move(world, fixed_today):
    group, s = alia_left(world, fixed_today, paid=0)
    assert facts(world, s, group, LEFT) == (200_000, 200_000, 0)
    fixed_today(date(2026, 11, 14))
    r = archive(world, group, expect=409)
    (blocker,) = [b for b in r.json()["detail"]["blockers"] if b["code"] == "OPEN_BALANCES"]
    assert "200000" in str(blocker) or "200 000" in str(blocker) or "200\xa0000" in str(blocker)                                                       # the sum of the debt is named
    assert facts(world, s, group, date(2026, 11, 14)) == (200_000, 200_000, 0)


def test_g4_a_leaver_of_october_keeps_the_charge_when_the_group_is_archived_in_november(world, fixed_today):
    # October 2026: Thu 1, 8, 15, 22, 29; Tue 6, 13, 20, 27; Fri 2, 9, 16, 23, 30 = 14 lessons.  Left 09.10 after Fri 02, Tue 06, Thu 08 and Fri 09 = 4 lessons: 600 000 x 4 / 14 = 171 428.57 (rounded by the ledger)
    fixed_today(date(2026, 10, 1))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    group = make_group(world.db, world.org, world.teacher, "Арх")
    give_group_price(world.db, group, 600_000)
    set_schedule(world, TTF, group)
    ensure_billing(world, group, price=None)
    s = fresh_student(world, "Октябрь")
    add(world, s, group=group, mode="add")
    fixed_today(date(2026, 10, 10))
    leave(world, group, s)
    before = facts(world, s, group, date(2026, 10, 10))
    assert before[0] > 0
    pay(world, s, group, amount=before[0], paid_on=date(2026, 10, 10))
    fixed_today(date(2026, 11, 14))
    archive(world, group)
    assert facts(world, s, group, date(2026, 11, 14)) == (before[0], 0, 0)
    fixed_today(date(2026, 11, 18))
    restore(world, group)
    assert facts(world, s, group, date(2026, 11, 18)) == (before[0], 0, 0)


def test_g5_personal_period_and_a_package_are_not_touched_by_the_archive(world, fixed_today):
    # a personal period (billing_anchor = payment date): 600 000 for 30 days, left on the 11th day of the period -> the charge is by the days of the stay; the archive of the group does not change it
    fixed_today(date(2026, 11, 2))
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="payment_date")
    group = make_group(world.db, world.org, world.teacher, "Арх")
    give_group_price(world.db, group, 600_000)
    set_schedule(world, TTF, group)
    ensure_billing(world, group, price=None)
    s = fresh_student(world, "Личный")
    add(world, s, group=group, mode="add")
    fixed_today(LEFT)
    leave(world, group, s)
    before = facts(world, s, group, LEFT)
    assert before[0] > 0
    fixed_today(date(2026, 11, 14))
    archive(world, group, expect=409 if before[1] else 200)
    if not before[1]:
        fixed_today(date(2026, 11, 18))
        restore(world, group)
    assert facts(world, s, group, date(2026, 11, 18)) == before


def test_g6_the_archive_of_a_student_keeps_the_closed_stay_after_the_return(world, fixed_today):
    # the archive of the STUDENT (not of the group) in the middle of the month closes his stay as a leaving does; returning from the archive does not change the charge of the closed stay
    group = tue_world(world, fixed_today)
    s = fresh_student(world, "Архивный")
    add(world, s, group=group, mode="add")
    fixed_today(LEFT)
    r = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
    assert r.status_code == 200, r.text
    charged = facts(world, s, group, LEFT)[0]
    assert charged == 200_000                                                              # lessons 3, 5, 6, 10 of 12
    fixed_today(date(2026, 11, 16))
    r = owner(world).post(f"{API}/org/students/{s.id}/restore", json={})
    assert r.status_code == 200, r.text
    assert facts(world, s, group, date(2026, 11, 16))[0] == 200_000                       # the closed stay is the same after the return (a return opens a NEW stay)


# ---------------------------------------------------------------------------------------------------- the exceptions of А6.8 (written as they are)


def test_x1_a_leaving_on_the_day_of_the_archive_loses_only_the_lesson_that_did_not_take_place(world, fixed_today):
    # Alia is in the group from 02.11; she leaves on Fri 20.11 at 12:00 and the group is archived the same day at 12:00: the lesson of 20.11 (14:00) never took place.
    # Lessons 3, 5, 6, 10, 12, 13, 17, 19 = 8 (+ the 20th while the group lives: 9) -> 600 000 x 9 / 12 = 450 000 before the archive, 600 000 x 8 / 12 = 400 000 after (the lesson that did not take place is not in the numerator);
    # the denominator stays 12 (before the fix: 11 and 436 000-ish)
    group = tue_world(world, fixed_today)
    s = fresh_student(world, "Ушла в день архива")
    add(world, s, group=group, mode="add")
    fixed_today(date(2026, 11, 20))
    leave(world, group, s)
    before = facts(world, s, group, date(2026, 11, 20))
    assert before[0] in (400_000, 450_000)                                                  # the rule of the leaving day (G-8) decides whether the 20th is in; the fact is pinned below
    pay(world, s, group, amount=before[0], paid_on=date(2026, 11, 20))
    archive(world, group)
    led = account(world, membership(world, s, group).id, date(2026, 11, 20))
    (seg,) = [x for x in led.segments if x.kind == "period"]
    assert seg.lessons_in_period == 12 and led.charged == 400_000                          # 8 real lessons / the full month
    assert (led.debt, led.advance) == (0, before[0] - 400_000)


def test_x2_a_student_written_back_into_a_past_archive_interval_counts_only_real_lessons(world, fixed_today):
    # archived 14.11, restored 18.11; a stay written back to 16.11 (Mon): the lesson of Tue 17.11 did not exist (the group slept), so the numerator is 19, 20, 24, 26, 27 = 5 of 12 -> 250 000
    group, s = alia_left(world, fixed_today)
    fixed_today(date(2026, 11, 14))
    archive(world, group)
    fixed_today(date(2026, 11, 18))
    restore(world, group)
    b = fresh_student(world, "Задним числом")
    fixed_today(date(2026, 11, 19))
    add(world, b, group=group, mode="add")
    stay(world, membership(world, b, group), date(2026, 11, 16))
    led = account(world, membership(world, b, group).id, date(2026, 11, 19))
    (seg,) = [x for x in led.segments if x.kind == "period"]
    assert (seg.amount, seg.lessons_in_period) == (250_000, 12)


def test_x3_an_archive_without_an_interval_in_old_data_changes_nothing(world, fixed_today):
    # a group archived by an old image: status archived, no interval recorded -> the schedule is whole, the charge of the leaver is the same 200 000
    group, s = alia_left(world, fixed_today)
    group.status = "archived"
    group.inactive_intervals = []
    flag_modified(group, "inactive_intervals")
    world.db.commit()
    assert facts(world, s, group, date(2026, 11, 20)) == (200_000, 0, 0)
