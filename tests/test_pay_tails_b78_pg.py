"""PAY-TAILS Б7.8 step 4.2, through the routes of the product (PostgreSQL): the account of a PACKAGE carries the lessons of the days of a pause, and the ledger takes the days of the pauses it knows out (one place;
the world known when a forgiveness was entered is built by the same code as today's).  The pure twin with the numbers of the spec is ``test_pay_tails_b78.py`` (600 000 forgiven on 21.10).

What the product allows (found while writing this test): a pause over a day on which the student has a COUNTED lesson is refused (``PAUSE_OVERLAPS_ATTENDED``, ``logica_service.py:566``), so a pause entered
later never takes a counted lesson out of a package through the route; the lessons of a pause days the account meets are those whose roster snapshot was made before the pause (the pause is entered, the lesson of its
day is held for a student who stood in the snapshot).  The product also refuses a forgiveness above the debt of the day (here 400 000 on 21.10: only package 1 is charged by then).

World: packages of 8 lessons, 50 000 a lesson, Tue / Thu / Fri, the student joined 01.10.  Package 1 = 01.10 .. 16.10 (8 lessons, 400 000), package 2 would begin with the lesson of 20.10."""
from datetime import date, timedelta

from src.auth import billing_ledger as bl

from tests.money1b_helpers import add, fresh_student, hold_lessons, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)
from tests.test_pay_tails_money_pg import check, forgive

pytestmark = needs_pg
D = date
TTF = [slot("s2", 2, "2026-01-01", "14:00", "15:00"), slot("s4", 4, "2026-01-01", "14:00", "15:00"), slot("s5", 5, "2026-01-01", "14:00", "15:00")]


def days(first, last):
    return [d for d in (first + timedelta(days=k) for k in range((last - first).days + 1)) if d.isoweekday() in (2, 4, 5)]


def centre_of(world, fixed_today):
    fixed_today(D(2026, 10, 1))
    settings_block(world, accounting_start_date="2026-09-01", period_mode="lessons", lessons_per_payment=8, billing_anchor="payment_date")
    g = make_group(world.db, world.org, world.teacher, "Пакеты")
    give_group_price(world.db, g, 400_000)
    set_schedule(world, TTF, g)
    ensure_billing(world, g, price=None)
    s = fresh_student(world, "Ученик пакетов")
    add(world, s, group=g, mode="add")
    return g, s


def pause_of(world, s, g, first, last, expect=201):
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": first, "last_day": last, "reason": "Болезнь"})
    assert r.status_code == expect, r.text
    return r


def test_b78_a_pause_over_a_counted_lesson_is_refused_and_the_package_stays_as_it_was(world, fixed_today):
    # 01.10 .. 20.10: nine lessons held (package 1 = 400 000 + the lesson of 20.10 = 50 000, package 2 begun).  21.10: 450 000 forgiven (the whole debt of the day).  Today 02.12.  A pause 20.10 - 21.10 would take
    # the lesson of 20.10 out of package 2 - the product refuses it (409, the lesson is counted): nothing moves.  Charged 1 350 000, used 450 000, debt 900 000.
    g, s = centre_of(world, fixed_today)
    hold_lessons(world, s, days(D(2026, 10, 1), D(2026, 10, 20)), group=g)
    fixed_today(D(2026, 10, 21))
    forgive(world, s, g, 450_000, expected_debt=450_000)
    hold_lessons(world, s, days(D(2026, 10, 21), D(2026, 12, 2)), group=g)
    fixed_today(D(2026, 12, 2))
    before = check(world, s, g, D(2026, 12, 2), charged=1_350_000, debt=900_000, advance=0, used=450_000, granted=450_000)
    r = pause_of(world, s, g, "2026-10-20", "2026-10-21", expect=409)
    assert r.json()["detail"]["code"] == "PAUSE_OVERLAPS_ATTENDED"
    after = check(world, s, g, D(2026, 12, 2), charged=1_350_000, debt=900_000, advance=0, used=450_000, granted=450_000)
    assert [x.forgiven for x in after.segments if x.kind == "package"][:2] == [x.forgiven for x in before.segments if x.kind == "package"][:2] == [400_000, 50_000]


def test_b78_a_lesson_held_on_a_day_of_a_pause_is_in_the_input_of_the_account_and_charges_nothing(world, fixed_today):
    # The pause 20.10 - 21.10 is entered on 15.10 (the days are ahead); the lesson of Tue 20.10 is held for the student who stood in its roster snapshot.  Lessons 01.10 .. 22.10 = 10 (1 2 6 8 9 13 15 16 20 22).
    # The loader gives the ledger ALL of them; the ledger charges the nine that are not on a day of the pause (the lesson of 20.10 is nobody's).
    from src.auth import ledger_service as ls
    from src.auth import payment_status as ps

    g, s = centre_of(world, fixed_today)
    fixed_today(D(2026, 10, 15))
    pause_of(world, s, g, "2026-10-20", "2026-10-21")
    hold_lessons(world, s, days(D(2026, 10, 1), D(2026, 10, 22)), group=g)
    fixed_today(D(2026, 10, 23))
    m = membership(world, s, g)
    world.db.expire_all()
    loader = ls.LedgerLoader(world.db, str(world.org.id), ps.business_today())
    loader.load_facts([m.id])
    inp = loader.build_input(m.id)
    assert D(2026, 10, 20) in {c.day for c in inp.counted_lessons}                         # the lesson of the pause is carried
    assert D(2026, 10, 20) not in {c.day for c in bl.run_ledger(inp).counted}              # and charges nothing
    assert len(inp.counted_lessons) == len(bl.run_ledger(inp).counted) + 1
