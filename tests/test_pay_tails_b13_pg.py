"""PAY-TAILS Б11.18 / Б11.19, step 8.13: the pause that went THERE AND BACK, the pause under a leaving, the window of a leaving after a cancelled one, the rule F1 by the moment of the record.  PostgreSQL, through the routes
of the product (LESSONS 135); every number is worked out by hand in the comment.  Every edit of a pause checks the FOUR numbers of lesson 168: the probe (``dry_run``), ``impact`` of the answer, ``row`` of the answer, the account after.

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1; November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each; the student since 01.11."""
from datetime import date
from uuid import UUID

from tests.money1b_helpers import add, fresh_student, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import TTF, check, forgive, leave, led
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date


def edit_pause(world, s, g, pause_id, today, first, last, *, debt, advance, charged, used, granted):
    """An edit of the pause and its four numbers: the probe, ``impact`` and ``row`` of the answer, the account after - the same debt and advance, and the charge / used forgiveness of the account."""
    body = {"first_day": first, "last_day": last, "reason": "Поправили"}
    probe = owner(world).patch(f"{API}/org/pauses/{pause_id}", params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    saved = owner(world).patch(f"{API}/org/pauses/{pause_id}", json=body)
    assert saved.status_code == 200, saved.text
    row, after = saved.json()["row"], led(world, s, g, today)
    said = lambda i: (i["debt_after"], i["advance_after"])
    assert said(probe.json()["impact"]) == said(saved.json()["impact"]) == (row["unpaid"], row["advance"]) == (after.debt, after.advance) == (debt, advance), ('GOT charged/used', after.charged, after.written_off, 'probe/impact/row/after (debt, advance)', said(probe.json()["impact"]), said(saved.json()["impact"]), (row["unpaid"], row["advance"]), (after.debt, after.advance))
    check(world, s, g, today, charged=charged, debt=debt, advance=advance, used=used, granted=granted)


def world_b11_18(world, fixed_today, *, forgive_first):
    # the student since 01.11; 05.11 forgiven 300 000 and paid 300 000 (the two orders): November 600 000 = 300 000 + 300 000, debt 0.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    if forgive_first:
        forgive(world, s, g, 300_000)
        pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
    else:
        pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
        forgive(world, s, g, 300_000)
    check(world, s, g, D(2026, 11, 5), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)
    return g, s


def run_b11_18_p1(world, fixed_today, forgive_first):
    g, s = world_b11_18(world, fixed_today, forgive_first=forgive_first)
    fixed_today(D(2026, 11, 10))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-11-30")                    # lessons 24, 26, 27 lost: November 450 000
    check(world, s, g, D(2026, 11, 10), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
    fixed_today(D(2026, 11, 11))                                                    # + 17, 19, 20: 300 000 = the money alone; the forgiveness has nothing to cover
    edit_pause(world, s, g, pause_id, D(2026, 11, 11), "2026-11-17", "2026-11-30", debt=0, advance=0, charged=300_000, used=0, granted=300_000)
    fixed_today(D(2026, 11, 12))                                                    # BACK to 24 - 30.11: the account as after 10.11 (the long form is not a fact any more)
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-24", "2026-11-30", debt=0, advance=0, charged=450_000, used=150_000, granted=300_000)
    fixed_today(D(2026, 11, 13))                                                    # shortened to 26 - 30.11 (lessons 26, 27): 500 000 = 300 000 money + 200 000 of the forgiveness; the same as a pause entered as 26 - 30.11
    edit_pause(world, s, g, pause_id, D(2026, 11, 13), "2026-11-26", "2026-11-30", debt=0, advance=0, charged=500_000, used=200_000, granted=300_000)


def test_b11_18_p1_a_pause_there_and_back_leaves_the_forgiveness_as_it_was_forgiven_first(world, fixed_today):
    run_b11_18_p1(world, fixed_today, True)


def test_b11_18_p1_a_pause_there_and_back_leaves_the_forgiveness_as_it_was_paid_first(world, fixed_today):
    run_b11_18_p1(world, fixed_today, False)


def test_b11_18_p1_guard_a_forgiveness_entered_under_the_long_pause_is_not_given_back_by_the_shortening(world, fixed_today):
    # 05.11 paid 200 000; 10.11 the pause 17 - 30.11 (November 300 000, debt 100 000); 11.11 forgiven 100 000 (the debt of that day); 12.11 the pause is shortened to 24 - 30.11 (November 450 000): the period got dearer AFTER
    # the forgiveness - the three lessons are new charges: debt 150 000, used 100 000, advance 0.  (A legal growth: the forgiveness covered what was owed THEN.)
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 10))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    check(world, s, g, D(2026, 11, 10), charged=300_000, debt=100_000, advance=0, used=0, granted=0)
    fixed_today(D(2026, 11, 11))
    forgive(world, s, g, 100_000)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-24", "2026-11-30", debt=150_000, advance=0, charged=450_000, used=100_000, granted=100_000)


def test_b11_19_world_z_a_payment_between_the_two_edits_keeps_its_place(world, fixed_today):
    # 05.11 forgiven 300 000 and paid 300 000; 07.11 the pause 24 - 30.11; 08.11 it is edited to 17 - 30.11; 09.11 the parent pays 100 000; 10.11 the pause is back to 24 - 30.11: November 450 000 = money 300 000 + forgiveness
    # 150 000; the payment of 100 000 is an advance.  Debt 0, advance 100 000, used 150 000.  (Was: debt 50 000.)
    g, s = world_b11_18(world, fixed_today, forgive_first=True)
    fixed_today(D(2026, 11, 7))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 8))
    edit_pause(world, s, g, pause_id, D(2026, 11, 8), "2026-11-17", "2026-11-30", debt=0, advance=0, charged=300_000, used=0, granted=300_000)
    fixed_today(D(2026, 11, 9))
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 9))
    fixed_today(D(2026, 11, 10))
    edit_pause(world, s, g, pause_id, D(2026, 11, 10), "2026-11-24", "2026-11-30", debt=0, advance=100_000, charged=450_000, used=150_000, granted=300_000)


# ---------------------------------------------------------------------------------------------------------------------------- the pause under a leaving, a transfer, an archive
def leave_numbers(world, s, g, today, expect):
    """The window of the leaving, its answer and the account after: the same charge, debt and advance."""
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    window = owner(world).get(f"{url}/leave-preview")
    assert window.status_code == 200, window.text
    probe = next(m for m in window.json()["memberships"] if m["membership_id"] == str(membership(world, s, g).id))["money"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    after = led(world, s, g, today)
    assert (probe["charged_after_leave"], probe["debt_amount"], probe["advance"]) == (after.charged, after.debt, after.advance) == expect[:3], (probe, after)
    assert after.written_off == expect[3] and after.refundable == expect[4]


def test_b11_19_guard_a_leaving_inside_a_pause_keeps_the_forgiveness_entered_before_the_leaving(world, fixed_today):
    # 05.11 paid 150 000; 09.11 the pause 17 - 30.11 (November 300 000, debt 150 000); 11.11 forgiven 100 000 and paid 450 000 (the debt 50 000 is paid, 400 000 is an advance); 22.11 the leaving cuts the pause to 17 - 22.11: the stay is
    # 01 - 22.11, the pause holds 17 - 22 (lessons 3 5 6 10 12 13 = 300 000).  Charged 300 000, the forgiveness 100 000 was entered when the debt was real: used 100 000, debt 0, advance (and to give back) 400 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 9))
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 11))
    forgive(world, s, g, 100_000)
    pay(world, s, g, amount=450_000, paid_on=D(2026, 11, 11))
    check(world, s, g, D(2026, 11, 11), charged=300_000, debt=0, advance=400_000, used=100_000, granted=100_000)
    fixed_today(D(2026, 11, 22))
    leave_numbers(world, s, g, D(2026, 11, 22), (300_000, 0, 400_000, 100_000, 400_000))


def there_and_back_before_a_leaving(world, fixed_today):
    # 05.11 forgiven 300 000 and paid 300 000; 07.11 the pause 24 - 30.11 (it has not begun on 20.11); 08.11 edited to 17 - 30.11; 09.11 back to 24 - 30.11.  Without the edits the leaving on 20.11 takes the pause off (it begins
    # after the day): the stay 01 - 20.11 = 9 lessons = 450 000 = money 300 000 + forgiveness 150 000; debt 0, advance 0, used 150 000.
    g, s = world_b11_18(world, fixed_today, forgive_first=True)
    fixed_today(D(2026, 11, 7))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-11-30")
    fixed_today(D(2026, 11, 8))
    edit_pause(world, s, g, pause_id, D(2026, 11, 8), "2026-11-17", "2026-11-30", debt=0, advance=0, charged=300_000, used=0, granted=300_000)
    fixed_today(D(2026, 11, 9))
    edit_pause(world, s, g, pause_id, D(2026, 11, 9), "2026-11-24", "2026-11-30", debt=0, advance=0, charged=450_000, used=150_000, granted=300_000)
    fixed_today(D(2026, 11, 20))
    return g, s


def test_b11_19_a_pause_there_and_back_then_a_leaving_before_its_start_leaves_no_debt(world, fixed_today):
    g, s = there_and_back_before_a_leaving(world, fixed_today)
    leave_numbers(world, s, g, D(2026, 11, 20), (450_000, 0, 0, 150_000, 0))


def test_b11_19_the_same_through_the_archive_of_the_student(world, fixed_today):
    g, s = there_and_back_before_a_leaving(world, fixed_today)
    r = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)


def test_b11_19_the_same_through_a_transfer(world, fixed_today):
    g, s = there_and_back_before_a_leaving(world, fixed_today)
    b = make_group(world.db, world.org, world.teacher, "Бета")
    give_group_price(world.db, b, 600_000)
    set_schedule(world, TTF, b)
    ensure_billing(world, b, price=None)
    add(world, s, group=b, mode="transfer", from_group_id=str(g.id))
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)


# ---------------------------------------------------------------------------------------------------------------------------- the other two modes of the account
def test_b11_19_the_lesson_cancelled_by_the_centre_inside_the_pause_changes_nothing_there_and_back(world, fixed_today):
    # The world of Б11.18 п. 1 with the setting «уроки, отменённые центром» on and Tuesday 24.11 cancelled by the centre on 10.11 (after the pause was entered).  The lesson lies INSIDE the pause 24 - 30.11 (and inside 17 - 30.11): it is
    # nobody's lesson, no deduction is made; the numbers are those of the world without the cancel: 450 000 / used 150 000 -> 300 000 / used 0 -> 450 000 / used 150 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 10))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-11-30")
    r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 10), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
    fixed_today(D(2026, 11, 11))
    edit_pause(world, s, g, pause_id, D(2026, 11, 11), "2026-11-17", "2026-11-30", debt=0, advance=0, charged=300_000, used=0, granted=300_000)
    fixed_today(D(2026, 11, 12))
    edit_pause(world, s, g, pause_id, D(2026, 11, 12), "2026-11-24", "2026-11-30", debt=0, advance=0, charged=450_000, used=150_000, granted=300_000)


def test_b11_19_packages_a_pause_over_future_lessons_there_and_back_keeps_the_package_as_it_was(world, fixed_today):
    # Packages of 8 lessons, 400 000 (50 000 a lesson); the student since 01.10; eight lessons held (1 2 6 8 9 13 15 16.10): the package 1 = 400 000.  16.10 forgiven 100 000 (the debt of the day) and paid 300 000: debt 0, used
    # 100 000.  20.10 a pause 27.10 - 10.11 (no lesson of it is held: the product refuses a pause over a counted lesson); 21.10 it is edited to 24.10 - 10.11; 22.10 back.  The package 1 is whole in every form: charged 400 000,
    # debt 0, advance 0, used 100 000 (the guard: through the routes a pause cannot reach a counted lesson, so the generator's reds of the packages - ``tests/pay_tails_b7_tools.py``, mode packages - have no route to).
    from tests.money1b_helpers import hold_lessons
    from tests.test_pay_tails_b78_pg import centre_of, days

    g, s = centre_of(world, fixed_today)
    hold_lessons(world, s, days(D(2026, 10, 1), D(2026, 10, 16)), group=g)
    fixed_today(D(2026, 10, 16))
    forgive(world, s, g, 100_000, expected_debt=400_000)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 10, 16))
    check(world, s, g, D(2026, 10, 16), charged=400_000, debt=0, advance=0, used=100_000, granted=100_000)
    fixed_today(D(2026, 10, 20))
    pause_id = pause_on(world, s, g, "2026-10-27", "2026-11-10")
    fixed_today(D(2026, 10, 21))
    edit_pause(world, s, g, pause_id, D(2026, 10, 21), "2026-10-24", "2026-11-10", debt=0, advance=0, charged=400_000, used=100_000, granted=100_000)
    fixed_today(D(2026, 10, 22))
    edit_pause(world, s, g, pause_id, D(2026, 10, 22), "2026-10-27", "2026-11-10", debt=0, advance=0, charged=400_000, used=100_000, granted=100_000)


# ---------------------------------------------------------------------------------------------------------------------------- Б11.18 п. 2 / Б11.19 п. 2: the closing «now» after a CANCELLED leaving
def second_leaving_world(world, fixed_today):
    # The world of step 8.12.1: 05.11 forgiven 300 000 (November: debt 300 000); 20.11 he leaves, is taken back the same day (the cancel of the leaving), the parent pays 300 000 (inside the debt).  The second leaving is NOT done.
    # What it will give (by hand): November 01 - 20.11 = 9 lessons = 450 000; the payment 300 000 was entered while he was in the group - BEFORE the second leaving - and the forgiveness 300 000 is cut to 150 000:
    # charged 450 000, debt 0, advance 0, to give back 0, used 150 000 of 300 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    add(world, s, group=g, mode="add")
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 20))
    return g, s


def account_after_the_second_leaving(world, s, g):
    after = check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
    assert after.refundable == 0
    return after


def said_nothing_about_the_money(effects):
    """The window / the answer has no line «аванс» / «долг» and no move of money: after the closing the account owes nothing and keeps nothing («Расчётов нет»; a transfer: «Переносить нечего», the sum 0)."""
    stays = [(x["code"], x.get("amount", 0)) for x in effects["stays"]]
    return bool(stays) and all(code == "none" or (code == "money_stays" and amount == 0) for code, amount in stays) and not [m for m in effects.get("moves", []) if m["code"] == "money"]


def test_b11_19_the_window_of_the_second_leaving_its_answer_and_the_account_after_are_the_same_numbers(world, fixed_today):
    g, s = second_leaving_world(world, fixed_today)
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    window = owner(world).get(f"{url}/leave-preview")
    assert window.status_code == 200, window.text
    (m,) = window.json()["memberships"]
    assert (m["money"]["charged_after_leave"], m["money"]["debt_amount"], m["money"]["advance"]) == (450_000, 0, 0), m["money"]
    assert said_nothing_about_the_money(m["effects"]), m["effects"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    assert said_nothing_about_the_money(done.json()["effects"]), done.json()["effects"]
    account_after_the_second_leaving(world, s, g)


def test_b11_19_an_administrator_without_the_right_to_money_leaves_him_there_is_no_advance_to_hide(world, fixed_today):
    from tests.pg_world import make_client

    g, s = second_leaving_world(world, fixed_today)
    admin = make_client(world, user=world.admin)
    r = admin.post(f"{API}/org/groups/{g.id}/students/{s.id}/leave", json={})
    assert r.status_code == 200, r.text                      # (was 403: the window of the check counted the payment of the cancel as entered after the leaving: advance 150 000)
    account_after_the_second_leaving(world, s, g)


def test_b11_19_the_window_of_the_archive_of_the_student_says_the_same(world, fixed_today):
    g, s = second_leaving_world(world, fixed_today)
    window = owner(world).get(f"{API}/org/students/{s.id}/archive-preview")
    assert window.status_code == 200, window.text
    (m,) = window.json()["memberships"]
    assert said_nothing_about_the_money(m["effects"]), m["effects"]
    done = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
    assert done.status_code == 200, done.text
    account_after_the_second_leaving(world, s, g)


def test_b11_19_the_window_of_a_transfer_says_the_same(world, fixed_today):
    g, s = second_leaving_world(world, fixed_today)
    b = make_group(world.db, world.org, world.teacher, "Бета")
    give_group_price(world.db, b, 600_000)
    set_schedule(world, TTF, b)
    ensure_billing(world, b, price=None)
    window = add(world, s, group=b, mode="transfer", from_group_id=str(g.id), dry_run=True)
    assert said_nothing_about_the_money(window["effects"]), window["effects"]
    done = add(world, s, group=b, mode="transfer", from_group_id=str(g.id))
    assert done["status"] == "added"
    account_after_the_second_leaving(world, s, g)


def test_b11_19_guard_the_archive_of_the_group_does_not_reach_a_student_who_is_in_it(world, fixed_today):
    # The closing of a group (``group_lifecycle``) reads the same account, but the route never closes a student: a group with an active student is refused (409 ``GROUP_ARCHIVE_BLOCKED``, the blocker ``ACTIVE_STUDENTS``) -
    # there is no world where the archive of a group carries the piece of a cancelled leaving.  Nothing is written.
    g, s = second_leaving_world(world, fixed_today)
    r = owner(world).post(f"{API}/org/groups/{g.id}/archive", json={"missedLessonsAction": "cancel"})
    assert r.status_code == 409 and [b["code"] for b in r.json()["detail"]["blockers"]] == ["ACTIVE_STUDENTS"], r.text
    assert membership(world, s, g).is_active


def test_b11_14_guard_an_ordinary_return_does_not_carry_the_moment_the_window_is_the_account(world, fixed_today):
    # The world A of Б11.14 (an ORDINARY return, no cancel): 05.11 paid 300 000; 20.11 he leaves (450 000, debt 150 000); the same day 150 000 are forgiven (dated 19.11); an ordinary return (``add``); the parent pays 150 000; the second leaving
    # the same day.  The first leaving is a real one: the forgiveness (entered after it) stays and the payment is the money of a stay that never began: charged 450 000, debt 0, used 150 000, advance 150 000.  The window
    # of the second leaving = the answer = the account after.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    forgive(world, s, g, 150_000, effective_on=D(2026, 11, 19))
    add(world, s, group=g, mode="add")
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 20))
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    (m,) = owner(world).get(f"{url}/leave-preview").json()["memberships"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    after = check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=150_000, used=150_000, granted=150_000)
    assert (m["money"]["charged_after_leave"], m["money"]["debt_amount"], m["money"]["advance"]) == (450_000, 0, 150_000) and after.refundable == 150_000


# ---------------------------------------------------------------------------------------------------------------------------- Б11.19 п. 3: the rule Ф1 by the MOMENT of the record and by the use in the account after the closing
def ordinary_return_then_leave(world, fixed_today, *, forgiven_before_return, paid_after, forgiven_on=None):
    # ОМ 600 000, Tue / Thu / Fri; since 01.11; 05.11 paid 300 000; 20.11 he leaves (01 - 20.11 = 9 lessons = 450 000, debt 150 000); then, the same day, in one of the two orders: 150 000 are forgiven (before the ordinary return or
    # after it) and the parent pays 150 000 (``paid_after``); the second leaving the same day.  The window of the second leaving is read before it.  A return after the leaving with NO forgiveness between is the CANCEL of the
    # leaving (the records after it count as made before the second leaving, Б11.12); the return is an ORDINARY one only when a forgiveness stands between (the cancel is forbidden then).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    if forgiven_before_return:
        forgive(world, s, g, 150_000, effective_on=forgiven_on)
    add(world, s, group=g, mode="add")
    if not forgiven_before_return:
        forgive(world, s, g, 150_000, effective_on=forgiven_on)
    if paid_after:
        pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 20))
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    (m,) = owner(world).get(f"{url}/leave-preview").json()["memberships"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    return g, s, m["money"]


def kept_forgiveness(world, s, g):
    from src.models.auth import StudentWriteoff

    rows = world.db.query(StudentWriteoff).filter(StudentWriteoff.membership_id == membership(world, s, g).id).all()
    return [(r.amount, r.voided_at is None) for r in rows]


def test_b11_15_f1_b_the_forgiveness_entered_before_the_return_is_the_record_of_the_real_stay_and_stays(world, fixed_today):
    # Б11.15 (was «as it is»: the forgiveness was cancelled by the DATE).  The forgiveness 150 000 was entered BEFORE the ordinary return - after the first, real leaving: it covers the real debt of that stay.  The payment 150 000
    # after the return is the money of a stay that never began.  Charged 450 000, debt 0, used 150 000, advance 150 000, to give back 150 000, the record of the forgiveness is not cancelled.  Window = answer = account.
    g, s, money = ordinary_return_then_leave(world, fixed_today, forgiven_before_return=True, paid_after=True)
    after = check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=150_000, used=150_000, granted=150_000)
    assert (money["charged_after_leave"], money["debt_amount"], money["advance"]) == (450_000, 0, 150_000) and after.refundable == 150_000
    assert kept_forgiveness(world, s, g) == [(150_000, True)]


def test_b11_19_f1_v_after_the_cancel_without_a_payment_the_forgiveness_of_the_real_debt_stays(world, fixed_today):
    # The return has no «Оплата с» and no forgiveness stands before it: it is the CANCEL of the leaving (a return the same day takes no «Оплата с»: 409 ``LEAVE_UNDO_NO_START``).  The forgiveness 150 000 is entered after it, no payment
    # (the guard of Money-2: a forgiveness of the REAL debt of an earlier stay is left): the account after the closing uses it: charged 450 000, debt 0 (the forgiveness covers 150 000), used 150 000, advance 0.
    g, s, money = ordinary_return_then_leave(world, fixed_today, forgiven_before_return=False, paid_after=False)
    after = check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=150_000)
    assert (money["charged_after_leave"], money["debt_amount"], money["advance"]) == (450_000, 0, 0) and after.refundable == 0
    assert kept_forgiveness(world, s, g) == [(150_000, True)]


def test_b11_19_f1_g_a_forgiveness_dated_back_but_entered_during_the_empty_stay_is_cancelled(world, fixed_today):
    # A new student of 20.11 (Friday: one lesson of 50 000 is charged today); the same day 50 000 are forgiven DATED 19.11 (back); the student is taken out the same day with no counted mark: the stay is of no day, nothing is
    # charged - the forgiveness would stand as an advance.  It was entered after the stay was opened, the account after the closing does not use it (placings 0): cancelled by the closing, whatever its date says.
    # Charged 0, debt 0, advance 0, used 0; the window says the same.
    g, s = om_at(world, fixed_today, D(2026, 11, 20))
    forgive(world, s, g, 50_000, effective_on=D(2026, 11, 19))
    url = f"{API}/org/groups/{g.id}/students/{s.id}"
    (m,) = owner(world).get(f"{url}/leave-preview").json()["memberships"]
    done = owner(world).post(f"{url}/leave", json={})
    assert done.status_code == 200, done.text
    after = led(world, s, g, D(2026, 11, 20))
    assert (m["money"]["charged_after_leave"], m["money"]["debt_amount"], m["money"]["advance"]) == (after.charged, after.debt, after.advance) == (0, 0, 0)
    assert (after.written_off, after.written_off_granted, after.refundable) == (0, 0, 0)
    assert kept_forgiveness(world, s, g) == [(50_000, False)]


def test_b11_19_f1_v_the_return_without_pay_from_is_the_cancel_and_the_payment_after_it_counts_before_the_leaving(world, fixed_today):
    # The same world but the return has no «Оплата с» and no forgiveness stands between: it is the CANCEL of the leaving (Б11.12: a record made between the cancel and the second leaving is made BEFORE the leaving).  The
    # parent has paid 300 000 + 150 000 = 450 000 = the charge of 01 - 20.11: nothing is owed, the forgiveness 150 000 entered after the cancel is not used by the account and is cancelled by the closing: charged 450 000,
    # debt 0, used 0, advance 0, to give back 0 - the family does not leave money at the centre.  Window = answer = account.
    g, s, money = ordinary_return_then_leave(world, fixed_today, forgiven_before_return=False, paid_after=True)
    after = check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=0, granted=0)
    assert (money["charged_after_leave"], money["debt_amount"], money["advance"]) == (450_000, 0, 0) and after.refundable == 0
    assert kept_forgiveness(world, s, g) == [(150_000, False)]


# ---------------------------------------------------------------------------------------------------------------------------- helpers of the tests of a cancelled leaving (Б11.26 - Б11.27)
def undo_line(world, pause_id):
    """The lines of the journal that the cancel of the leaving wrote for the pause: ``[(first_day, last_day)]`` (Б11.26 п. 1: also when the human changed the end of the cut and the cancel gave nothing back - the line says the days the pause has now)."""
    from src.auth import ledger_service as ls
    from src.models.auth import GroupAudit
    lines = [(a.snapshot["first_day"], a.snapshot["last_day"]) for a in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == ls.AUDIT_PAUSE_SET)
             .order_by(GroupAudit.created_at, GroupAudit.id) if a.snapshot.get("pause_id") == pause_id and a.snapshot.get("via") == "leave_undone"]
    return lines


def world_s2(world, fixed_today):
    # S2: 05.11 forgiven 300 000 and paid 300 000 (600 000 = the month); 09.11 the pause 17 - 30.11 (the lessons 17 19 20 24 26 27 are lost: November 300 000 = the money alone, the forgiveness 300 000 is not used).
    g, s = world_b11_18(world, fixed_today, forgive_first=True)
    fixed_today(D(2026, 11, 9))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    check(world, s, g, D(2026, 11, 9), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    return g, s, pause_id


# ---------------------------------------------------------------------------------------------------------------------------- Б11.27, step 8.13.10: the human took the pause off after the leaving cut it - the cancel of the leaving does not bring it back
def test_b11_27_the_leaving_cut_the_pause_the_human_took_it_off_and_the_leaving_is_cancelled_the_pause_stays_off(world, fixed_today):
    # 05.11 forgiven 300 000 and paid 300 000 (November 600 000); 09.11 the pause 17 - 30.11 (November 300 000: used 0, advance 0); 20.11 the leaving: the pause is cut to 17 - 20.11, the stay 01 - 20.11 = 300 000;
    # the same day the human takes the pause OFF with a reason of his own: «there was no pause» - 01 - 20.11 = lessons 3 5 6 10 12 13 17 19 20 = 9 x 50 000 = 450 000 = the money 300 000 + the forgiveness 150 000, debt 0;
    # the cancel of the leaving: the student is in the group, there is no pause, November 600 000 = the money 300 000 + the forgiveness 300 000, debt 0, advance 0.  The cancel does NOT write ``leave_undone`` for a pause
    # nobody restores and does not put it back.
    g, s, pause_id = world_s2(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 20), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    done = owner(world).post(f"{API}/org/pauses/{pause_id}/void", json={"reason": "Передумали"})
    assert done.status_code == 200, done.text
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=0, used=150_000, granted=300_000)
    add(world, s, group=g, mode="add")
    check(world, s, g, D(2026, 11, 20), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)
    from src.models.auth import MembershipPause
    row = world.db.get(MembershipPause, UUID(pause_id))
    assert row.voided_at is not None and undo_line(world, pause_id) == []
