"""PAY-TAILS step 8.14.15 B11.51 C2' (PostgreSQL): the text of a return when the period is already in the account - on the hands (the probe and the record), the day «Оплата с» and the sums are not touched.

The world: the group Tue/Thu/Fri, 600 000 a month (the common month); November 2026 has 12 lessons (Tue 3, 10, 17, 24; Thu 5, 12, 19, 26; Fri 6, 13, 20, 27) = 50 000 each.  The student joined 02.11 (charged from the first lesson 03.11) and leaves on Wed 25.11
(the lessons 3 .. 24.11 = 10 = 500 000).  Back on Thu 26.11 (the next day, no «Оплата с»): all 12 lessons = 600 000; the window says 600 000 - 500 000 = 100 000 (2 lessons).  With «Оплата с» 27.11 the lesson 26.11 is lost: 11 lessons = 550 000."""
from datetime import date as D

from tests.answers_dump import dump
from tests.money1b_helpers import add, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today, settings_block  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_money_pg import TTF, forgive, leave, led, lp

pytestmark = needs_pg

NEW = "Возврат без перерыва: ноябрь уже в счёте и заново не считается."
FIRST = "Оплата считается с 26.11.2026 — с первого занятия группы"


def left_on_25(w, fixed_today, joined=D(2026, 11, 2)):
    g, s = om_at(w, fixed_today, joined)
    fixed_today(D(2026, 11, 25))
    leave(w, g, s)
    return g, s


def both(w, s, g, **kw):
    """(the text of the probe, the immediate of the probe, the text of the record)."""
    probe = add(w, s, group=g, mode="add", dry_run=True, **kw)["billing"]
    done = add(w, s, group=g, mode="add", **kw)["billing"]
    return probe["text"], probe.get("immediate"), done["text"], done


def test_c2_the_next_day_without_pay_from_says_the_period_is_in_the_account_and_the_sums_are_the_same(world, fixed_today):
    g, s = left_on_25(world, fixed_today)
    assert led(world, s, g, D(2026, 11, 25)).charged == 500_000
    fixed_today(D(2026, 11, 26))
    probe, imm, record, done = both(world, s, g)
    assert probe == record == NEW
    assert (imm["amount"], imm["lessons"]) == (100_000, 2) and done["pay_from"] == "2026-11-26"      # the day «Оплата с» is the first lesson as before
    assert led(world, s, g, D(2026, 11, 26)).charged == 600_000
    dump("return_preview_no_break", {"billing": {"text": probe, "immediate": imm}})


def test_c2_an_explicit_pay_from_keeps_the_old_text_and_loses_the_lesson_of_the_gap(world, fixed_today):
    g, s = left_on_25(world, fixed_today)
    fixed_today(D(2026, 11, 26))
    probe, imm, record, done = both(world, s, g, pay_from="2026-11-27")
    assert probe == record == "Оплата считается с 27.11.2026: так выбрал сотрудник" and done["pay_from"] == "2026-11-27"
    assert led(world, s, g, D(2026, 11, 26)).charged == 550_000
    dump("return_preview_pay_from", {"billing": {"text": probe, "immediate": imm}})


def test_c2_left_on_31_10_back_on_01_11_is_a_new_month_the_text_stays(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 10, 15))
    fixed_today(D(2026, 10, 31))
    leave(world, g, s)
    fixed_today(D(2026, 11, 1))
    probe, imm, record, done = both(world, s, g)
    assert probe == record == "Оплата считается с 03.11.2026 — с первого занятия группы"       # November was not in the account before: it begins with the new stay


def test_c2_the_same_day_return_after_a_forgiveness_is_an_ordinary_return_and_says_so(world, fixed_today):
    # The cancel of the leaving is forbidden after a forgiveness: the return of the day of leaving is ordinary.  By hand: 500 000 owed at the leaving, 50 000 forgiven; back the same day (the first lesson 26.11): 12 lessons = 600 000,
    # the debt = 600 000 - 50 000 = 550 000.
    g, s = left_on_25(world, fixed_today)
    forgive(world, s, g, 50_000)
    probe, imm, record, done = both(world, s, g)
    assert probe == record == NEW and done["pay_from"] == "2026-11-26"
    after = led(world, s, g, D(2026, 11, 25))
    assert (after.charged, after.debt) == (600_000, 550_000)


def test_c2_a_personal_period_returned_inside_the_counted_period_says_it(world, fixed_today):
    # The personal period from the day of adding 02.11 (the group had no schedule then: no lesson in sight = today) to 01.12 (a month by the payment date); the student leaves 25.11, back 26.11: the same period (a bridge), 02.11-01.12 is in the account.
    fixed_today(D(2026, 11, 2))
    g, s = lp(world, fixed_today, joined=D(2026, 11, 2))
    set_schedule(world, TTF, g)
    fixed_today(D(2026, 11, 25))
    leave(world, g, s)
    fixed_today(D(2026, 11, 26))
    probe, imm, record, done = both(world, s, g)
    assert probe == record == "Возврат без перерыва: 02.11–01.12 уже в счёте и заново не считается."


def test_c2_a_package_account_keeps_the_old_text(world, fixed_today):
    fixed_today(D(2026, 11, 2))
    settings_block(world, accounting_start_date="2026-10-01", period_mode="lessons", lessons_per_payment=12)
    g = make_group(world.db, world.org, world.teacher, "Пакеты")
    give_group_price(world.db, g, 600_000)
    set_schedule(world, TTF, g)
    ensure_billing(world, g, price=None)
    from tests.money1b_helpers import fresh_student

    s = fresh_student(world)
    first = add(world, s, group=g, mode="add")["billing"]["text"]
    assert first == "Оплата считается с 03.11.2026 — с первого занятия группы"        # the first adding: as before
    fixed_today(D(2026, 11, 25))
    leave(world, g, s)
    fixed_today(D(2026, 11, 26))
    probe, imm, record, done = both(world, s, g)
    assert probe == record == FIRST
