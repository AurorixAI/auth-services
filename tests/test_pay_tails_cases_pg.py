"""PAY-TAILS А7.10: the scenarios that had only a pure test or none, on the routes of the product (numbers by hand in the comments).  С11 (a package centre, a certificate through the requests route), С13
(the opening balance corrected through the openings route), С12 (a price lowered backdated), a payment entered backdated after a forgiveness."""
from datetime import date

from tests.pg_world import ensure_billing, make_client, needs_pg, payments_block, pg_session, reconcile, save, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today  # noqa: F401  (fixture)
from tests.test_pay_tails_money_pg import D, check, forgive, led, lp, membership, pay
from tests.test_payments_service_pg import mid, owner

pytestmark = needs_pg
API = "/api/v1/auth"


def test_c11_a_certificate_through_the_requests_route_makes_a_lesson_not_counted_and_the_forgiveness_covers_only_what_is_left(world):
    # Package centre: 500 000 for 10 lessons = 50 000 a lesson.  s1 attends two lessons and is «excused» (pending) at the third: three lessons are counted = 150 000 charged.  Forgiven 150 000: debt 0, used 150 000.
    # The request «по справке» for the third lesson is CONFIRMED through the route: two lessons are counted = 100 000 charged.  The forgiveness uses 100 000 (the other 50 000 are thrown away: NOT a paid
    # lesson): charged 100 000, debt 0, advance 0, refundable 0, used 100 000 of 150 000, lessons covered 2 / left 0.
    from tests.test_excuse_deadline import listing

    payments_block(world.db, world.org, period_mode="lessons", lessons_per_payment=10, billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 10}])
    ensure_billing(world, price=500_000)
    s1, s2, s3 = world.students
    for offset in (3, 2):
        day, lesson = reconcile(world, offset)
        save(world, day, lesson, [(s1, "present"), (s2, "present"), (s3, "present")], status="completed")
    day, lesson = reconcile(world, 1)
    save(world, day, lesson, [(s1, "excused"), (s2, "present"), (s3, "present")], status="completed", excuseReasons={s1.id: "Болел, справка"})
    from tests.money1b_helpers import account

    m = mid(world, s1)
    three = account(world, m)
    assert (three.charged, three.debt, three.lessons.used) == (150_000, 150_000, 3)
    r = owner(world).post(f"{API}/org/memberships/{m}/writeoffs", json={"reason": "Договорённость", "all": True})
    assert r.status_code == 201, r.text
    after_forgive = account(world, m)
    assert (after_forgive.debt, after_forgive.written_off, after_forgive.written_off_granted) == (0, 150_000, 150_000)
    mine = [i for i in listing(world) if i["student"]["id"] == str(s1.id)]
    assert len(mine) == 1, mine
    r = owner(world).post(f"{API}/org/attendance/excuses/{mine[0]['id']}/review", json={"decision": "confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed", r.text
    two = account(world, m)
    assert (two.charged, two.debt, two.advance, two.refundable, two.written_off, two.written_off_granted) == (100_000, 0, 0, 0, 100_000, 150_000), two
    assert (two.lessons.covered, two.lessons.used, two.lessons.left) == (2, 2, 0)
    assert two.reconciles and not two.flags


def test_c13_an_opening_debt_forgiven_then_corrected_down_through_the_openings_route(world, fixed_today):
    # ЛП 420 000 from 01.11, today 05.11.  The opening balance says «owed 100 000 on 30.09»: debt 100 000 + November 420 000 = 520 000.  Forgiven 100 000 (the oldest debt: the opening).  Then the opening is CORRECTED to
    # 60 000 through the openings route: 60 000 close the opening, the other 40 000 of the forgiveness lie on November (begun on 01.11): «простили 100 000 самого старого долга» - used 100 000, granted 100 000.
    # Charged 60 000 + 420 000 = 480 000, debt 480 000 - 100 000 = 380 000, advance 0.
    g, s = lp(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    m = membership(world, s, g)
    assert owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"debt_amount": 100_000, "debt_on": "2026-09-30"}).status_code == 200
    forgive(world, s, g, 100_000)
    check(world, s, g, D(2026, 11, 5), charged=520_000, debt=420_000, advance=0, used=100_000, granted=100_000)
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"debt_amount": 60_000, "debt_on": "2026-09-30", "reason": "Исправление остатка"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 5), charged=480_000, debt=380_000, advance=0, used=100_000, granted=100_000)


def test_c12_a_price_lowered_backdated_lowers_the_used_forgiveness_and_gives_no_advance(world, fixed_today):
    # ЛП 420 000 from 01.11: paid 200 000 (01.11), forgiven 220 000 (02.11): money 200 000 + forgiveness 220 000 = the price.  10.11: the owner CORRECTS the price in force (the row «since always») to 300 000 (the route allows a
    # backdated price only as a correction of a row with the same date and a reason: the student has money for the period).  Charged 300 000: money 200 000 first, the forgiveness covers the other 100 000 (used 100 000 of 220 000 - it GOES DOWN);
    # the 120 000 that are no longer needed are neither money nor an advance: advance 0, debt 0.  (O2 is the other order of the records, with its own number.)
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 2))
    forgive(world, s, g, 220_000)
    check(world, s, g, D(2026, 11, 2), charged=420_000, debt=0, advance=0, used=220_000, granted=220_000)
    fixed_today(D(2026, 11, 10))
    r = owner(world).put(f"{API}/org/groups/{g.id}/price", json={"amount": 300_000, "valid_from": "2000-01-01", "correction": True, "correction_reason": "Опечатка в цене"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 10), charged=300_000, debt=0, advance=0, used=100_000, granted=220_000)


def test_a_payment_entered_backdated_after_the_forgiveness_pays_the_period_the_forgiveness_could_not_reach(world, fixed_today):
    # ЛП 420 000 from 01.10 (accounting from 01.09).  25.10: October is forgiven (420 000).  26.10: the cashier enters a payment of 420 000 DATED 01.10.  05.11: a payment of 420 000.  By the order of the dates
    # the payment of 01.10 closes October, the forgiveness (25.10) would fall on November, which had not begun on 25.10: it is not used; the payment of 05.11 pays November.  Today 10.11: charged 840 000, debt 0,
    # advance 0, used 0 of 420 000 (the forgiveness was not needed - the statement says «использовано 0 сум»).
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1), accounting="2026-09-01")
    fixed_today(D(2026, 10, 25))
    forgive(world, s, g)
    fixed_today(D(2026, 10, 26))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 10, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 10))
    L = check(world, s, g, D(2026, 11, 10), charged=840_000, debt=0, advance=0, used=0, granted=420_000)
    octo, nov = [x for x in L.segments if x.kind == "period"][:2]
    assert (octo.money, octo.forgiven, nov.money, nov.forgiven) == (420_000, 0, 420_000, 0)
