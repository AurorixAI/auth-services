"""PAY-TAILS Б11.8 п. 9 step 8.6: a «typo» of a price corrected BACKDATED must not turn a forgiven debt into an advance that can be paid back.  PostgreSQL, through the routes of the product.

World (ОМ): November 600 000 (Tue / Thu / Fri, joined 02.11, 12 lessons, 50 000 each; the price row «since always»).  05.11 forgiven 300 000; 10.11 paid 300 000; today 12.11 the owner «corrects» the
price to 300 000 since 01.09 (the correction of the row of the same date).  Charged falls to 300 000: the money alone covers it, the forgiveness that was used (300 000) is not needed any more and the
other 300 000 of the money are an advance that the family could take back in cash - the debt that was forgiven has become an overpayment.  The correction is refused: 409 ``PRICE_CORRECTION_FORGIVENESS``.
Without the forgiveness (paid 300 000, nothing forgiven: debt 300 000 -> 0) the correction passes as before."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_money_pg import check, forgive, om

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
