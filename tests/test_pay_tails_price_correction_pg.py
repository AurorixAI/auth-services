"""PAY-TAILS Б11.8 п. 9 step 8.6: a «typo» of a price corrected BACKDATED must not turn a forgiven debt into an advance that can be paid back.  PostgreSQL, through the routes of the product.

World (ОМ): November 600 000 (Tue / Thu / Fri, joined 02.11, 12 lessons, 50 000 each; the price row «since always»).  05.11 forgiven 300 000; 10.11 paid 300 000; today 12.11 the owner «corrects» the
price to 300 000 since 01.09 (the correction of the row of the same date).  Charged falls to 300 000: the money alone covers it, the forgiveness that was used (300 000) is not needed any more and the
other 300 000 of the money are an advance that the family could take back in cash - the debt that was forgiven has become an overpayment.  The correction is refused: 409 ``PRICE_CORRECTION_FORGIVENESS``.
Without the forgiveness (paid 300 000, nothing forgiven: debt 300 000 -> 0) the correction passes as before."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_money_pg import check, forgive, led, om

pytestmark = needs_pg
D = date
TEXT = "У 1 ученика исправление превратит прощённый долг в переплату. Сначала отмените прощение долга, затем исправьте цену."
CORRECTION = {"amount": 300_000, "valid_from": "2000-01-01", "correction": True, "correction_reason": "Опечатка в цене"}


def forgiven_world(w, fixed_today, *, forgiven=300_000):
    g, s = om(w, fixed_today)
    fixed_today(D(2026, 11, 5))
    if forgiven:
        forgive(w, s, g, forgiven)
    fixed_today(D(2026, 11, 10))
    pay(w, s, g, amount=300_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    return g, s


def test_a_correction_of_the_group_price_that_turns_a_forgiven_debt_into_an_advance_is_refused(world, fixed_today):
    g, s = forgiven_world(world, fixed_today)
    for params in ({"dry_run": "true"}, {}):                                       # the window and the save say the same
        r = owner(world).put(f"{API}/org/groups/{g.id}/price", params=params, json=CORRECTION)
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["code"] == "PRICE_CORRECTION_FORGIVENESS" and d["message"] == TEXT and d["count"] == 1 and len(d["students"]) == 1, d
    check(world, s, g, D(2026, 11, 12), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)      # nothing was written


def test_a_correction_without_a_forgiveness_passes_as_before(world, fixed_today):
    # paid 300 000, nothing forgiven: debt 300 000; the price 300 000: charged 300 000, debt 0, advance 0.
    g, s = forgiven_world(world, fixed_today, forgiven=0)
    r = owner(world).put(f"{API}/org/groups/{g.id}/price", json=CORRECTION)
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=0, granted=0)


def test_a_correction_of_the_individual_price_goes_through_the_same_check(world, fixed_today):
    # The individual price of the student is first the group's 600 000 (a row of the same date, «since always»): correcting THAT row to 300 000 is the same correction of the same world.
    g, s = forgiven_world(world, fixed_today)
    m = membership(world, s, g)
    put = lambda body, **kw: owner(world).put(f"{API}/org/memberships/{m.id}/price-override", json=body, **kw)
    first = put({"amount": 600_000, "valid_from": "2000-01-01", "reason": "договор", "confirm_unusual": True})
    assert first.status_code == 200, first.text
    r = put({"amount": 300_000, "valid_from": "2000-01-01", "reason": "договор", "correction": True, "correction_reason": "Опечатка в цене", "confirm_unusual": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_CORRECTION_FORGIVENESS" and r.json()["detail"]["message"] == TEXT, r.text


# ---------------------------------------------------------------------------------------------------------------------------- Б11.11 п. 1: the condition by PLACES
# The worlds Ц1 - Ц5 of the spec, moved one month later onto the world ОМ (the student joins 02.11, so «October» = November and «November» = December; Tue / Thu / Fri, price 600 000 a month).
# The rule: refuse when for a place p of a membership N_after[p] < N_before[p] and forgiveness_after[p] > max(0, N_after[p] - money_before[p]) (N = charged minus the deductions of the place).


def months(w, s, g, today):
    """``[(charged, money, forgiven)]`` of the periods of the account, in order: the numbers of a month that the account shows."""
    L = led(w, s, g, today)
    return [(x.amount, x.money, x.forgiven) for x in sorted(L.segments, key=lambda x: x.period_start) if x.begun]


def refused(w, g, body=CORRECTION):
    for params in ({"dry_run": "true"}, {}):                                       # the window and the save say the same
        r = owner(w).put(f"{API}/org/groups/{g.id}/price", params=params, json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_CORRECTION_FORGIVENESS" and r.json()["detail"]["message"] == TEXT, (params, r.text)


def passes(w, g, body=CORRECTION):
    probe = owner(w).put(f"{API}/org/groups/{g.id}/price", params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    r = owner(w).put(f"{API}/org/groups/{g.id}/price", json=body)
    assert r.status_code == 200, r.text


def test_c1_a_forgiveness_that_keeps_its_place_and_pushes_the_money_out_is_refused(world, fixed_today):
    # November 600 000: forgiven 300 000 (05.11), paid 300 000 (10.11); the price -> 300 000 since always.  Before: money 300 000 + forgiven 300 000, debt 0, advance 0, used 300 000.
    # If it passed: charged 300 000, the older forgiveness keeps 300 000, the money 300 000 -> advance (debt 0, advance 300 000, used 300 000; November money 0 / forgiven 300 000).
    # Place November: N 300 000 < 600 000 and forgiven 300 000 > max(0, 300 000 - 300 000) = 0 -> refused.  (The old rule refused too.)
    g, s = forgiven_world(world, fixed_today)
    assert months(world, s, g, D(2026, 11, 12)) == [(600_000, 300_000, 300_000)]
    refused(world, g)
    assert months(world, s, g, D(2026, 11, 12)) == [(600_000, 300_000, 300_000)]


def test_c2_a_month_that_does_not_get_cheaper_with_a_forgiveness_passes(world, fixed_today):
    # November: forgiven 100 000 (05.11), paid 500 000 (10.11); December paid 600 000 (02.12); today 03.12; the price -> 500 000 since 01.12.
    # Before: charged 1 200 000, money 1 100 000, forgiven 100 000, debt 0, advance 0, used 100 000; November (600 000 / 500 000 / 100 000), December (600 000 / 600 000 / 0).
    # The place November does not get cheaper; December: N 500 000 < 600 000 but forgiven_after 0 <= 0.  After: charged 1 100 000, December (500 000 / 500 000 / 0): debt 0, advance 100 000 (money of the
    # family), used 100 000.  (The old rule: used > 0 and the advance grew 0 -> 100 000: a false refusal.)
    g, s = om(world, fixed_today)
    for amount in (700_000, 600_000):                                              # a row «since 01.12» with the same sum as before (it is what the correction will correct): entered in a future date twice
        row = owner(world).put(f"{API}/org/groups/{g.id}/price", json={"amount": amount, "valid_from": "2026-12-01"})
        assert row.status_code == 200, row.text
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 100_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=500_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 12, 2))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 12, 2))
    fixed_today(D(2026, 12, 3))
    check(world, s, g, D(2026, 12, 3), charged=1_200_000, debt=0, advance=0, used=100_000, granted=100_000)
    assert months(world, s, g, D(2026, 12, 3)) == [(600_000, 500_000, 100_000), (600_000, 600_000, 0)]
    passes(world, g, {"amount": 500_000, "valid_from": "2026-12-01", "correction": True, "correction_reason": "Опечатка в цене"})
    check(world, s, g, D(2026, 12, 3), charged=1_100_000, debt=0, advance=100_000, used=100_000, granted=100_000)
    assert months(world, s, g, D(2026, 12, 3)) == [(600_000, 500_000, 100_000), (500_000, 500_000, 0)]


def test_c3_a_forgiven_month_cut_to_the_new_price_passes(world, fixed_today):
    # November forgiven whole 600 000 (05.11), December paid 600 000 (02.12); today 03.12; the price -> 300 000 since always.  Before: charged 1 200 000, debt 0, advance 0, used 600 000;
    # (600 000 / 0 / 600 000), (600 000 / 600 000 / 0).  After: November 300 000 - forgiven 300 000 (what lies above burns: used 300 000), December 300 000 - money 300 000, the other 300 000 of the money are an
    # advance (the money of December, not in a month).  Debt 0, advance 300 000, used 300 000.  Place November: forgiven 300 000 <= max(0, 300 000 - 0) = 300 000.  (The old rule: a false refusal.)
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g)
    fixed_today(D(2026, 12, 2))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 12, 2))
    fixed_today(D(2026, 12, 3))
    check(world, s, g, D(2026, 12, 3), charged=1_200_000, debt=0, advance=0, used=600_000, granted=600_000)
    assert months(world, s, g, D(2026, 12, 3)) == [(600_000, 0, 600_000), (600_000, 600_000, 0)]
    passes(world, g)
    check(world, s, g, D(2026, 12, 3), charged=600_000, debt=0, advance=300_000, used=300_000, granted=600_000)
    assert months(world, s, g, D(2026, 12, 3)) == [(300_000, 0, 300_000), (300_000, 300_000, 0)]          # December: the money 300 000 pays it, the other 300 000 are not in a month - the advance


def test_c4_the_money_pushed_out_by_a_forgiveness_that_pays_the_next_month_is_refused(world, fixed_today):
    # The world of Ц1 and the clock on 03.12: December has begun and is not paid.  Before: charged 1 200 000, paid 300 000, forgiven 300 000: debt 600 000, advance 0, used 300 000;
    # (600 000 / 300 000 / 300 000), (600 000 / 0 / 0).  If it passed: charged 600 000; November keeps the forgiveness 300 000 and the money 300 000 goes to December (300 000): debt 0, advance 0, used 300 000 -
    # the forgiveness of November has paid December.  Place November: 300 000 > max(0, 300 000 - 300 000) = 0 -> refused.  (The old rule: the advance does not grow - it passed.)
    g, s = forgiven_world(world, fixed_today)
    fixed_today(D(2026, 12, 3))
    check(world, s, g, D(2026, 12, 3), charged=1_200_000, debt=600_000, advance=0, used=300_000, granted=300_000)
    assert months(world, s, g, D(2026, 12, 3)) == [(600_000, 300_000, 300_000), (600_000, 0, 0)]
    refused(world, g)
    check(world, s, g, D(2026, 12, 3), charged=1_200_000, debt=600_000, advance=0, used=300_000, granted=300_000)


def test_c5_money_first_then_the_forgiveness_burns_and_the_correction_passes(world, fixed_today):
    # November: paid 300 000 (03.11), then forgiven 300 000 (05.11); the price -> 300 000 since always.  Before: money 300 000 + forgiven 300 000, debt 0, advance 0, used 300 000.
    # After: charged 300 000: the money (the older) covers it, the forgiveness has no place and burns: debt 0, advance 0, used 0.  Place November: forgiven_after 0 <= max(0, 300 000 - 300 000) = 0.
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 12))
    check(world, s, g, D(2026, 11, 12), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)
    assert months(world, s, g, D(2026, 11, 12)) == [(600_000, 300_000, 300_000)]
    passes(world, g)
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    assert months(world, s, g, D(2026, 11, 12)) == [(300_000, 300_000, 0)]
