"""PAY-TAILS Б11.4 step 8.5 (lesson 164): «the probe = the record» for every route of a pause that answers a probe (``?dry_run=true``).  The probe answer, the answer of the save and the ledger read
afterwards say the SAME numbers (debt, advance, the change of the charge) - and the numbers are worked out by hand.  PostgreSQL, through the routes of the product.

World (ОМ, as ``test_pay_tails_b77_pg``): 600 000 a month, Tue / Thu / Fri, joined 02.11; November has 12 lessons, 50 000 each.  Pause 24.11 - 30.11 entered 03.11 (3 lessons lost: 450 000)."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_b77_pg import extend, pause_to_edit
from tests.test_pay_tails_money_pg import check, forgive, led, om

pytestmark = needs_pg
D = date


def numbers(impact):
    return impact["debt_after"], impact["advance_after"], impact["refundable_after"]


def test_chain_1_the_probe_of_an_edit_the_edit_and_the_ledger_after_say_the_same(world, fixed_today):
    # 05.11 forgiven 225 000 (half of 450 000); 10.11 paid 225 000; 12.11 the pause is EXTENDED to 17.11 - 30.11 (November 300 000, 6 lessons lost).  The edit is the latest fact: 450 000 - 300 000 =
    # 150 000 are cut off the forgiveness (alive 75 000); money 225 000 + forgiveness 75 000 = 300 000.  Debt 0, advance 0, used 75 000.  (Was: the probe and the answer of the save said advance 150 000.)
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 225_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=225_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    body = {"first_day": "2026-11-17", "reason": "Болезнь продлилась"}
    probe = owner(world).patch(f"{API}/org/pauses/{pause_id}", params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    saved = owner(world).patch(f"{API}/org/pauses/{pause_id}", json=body)
    assert saved.status_code == 200, saved.text
    assert numbers(probe.json()["impact"]) == numbers(saved.json()["impact"]) == (0, 0, 0), (probe.json()["impact"], saved.json()["impact"])
    assert probe.json()["impact"]["charged_delta"] == saved.json()["impact"]["charged_delta"] == -150_000
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=75_000, granted=225_000)


def test_the_probe_of_a_new_pause_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # November 600 000 (no pause yet); 05.11 forgiven 300 000; 10.11 paid 300 000; 12.11 a pause 17.11 - 30.11 is ENTERED (6 lessons lost, November 300 000): the money alone covers it - the forgiveness
    # (entered earlier than the pause) is cut to nothing.  Debt 0, advance 0, used 0.
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    body = {"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"}
    url = f"{API}/org/memberships/{membership(world, s, g).id}/pauses"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 201, probe.text
    saved = owner(world).post(url, json=body)
    assert saved.status_code == 201, saved.text
    assert numbers(probe.json()["impact"]) == numbers(saved.json()["impact"]) == (0, 0, 0), (probe.json()["impact"], saved.json()["impact"])
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=0, granted=300_000)


def test_the_probe_of_taking_a_pause_off_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # Pause 24.11 - 30.11 (November 450 000); 05.11 forgiven 225 000; 10.11 paid 225 000; 12.11 the pause is TAKEN OFF («the pause never was», С5): November 600 000.  Money 225 000 + forgiveness
    # 225 000 = 450 000; debt 150 000, advance 0.
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 225_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=225_000, paid_on=D(2026, 11, 10))
    fixed_today(D(2026, 11, 12))
    body = {"reason": "Поставили по ошибке"}
    url = f"{API}/org/pauses/{pause_id}/void"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    saved = owner(world).post(url, json=body)
    assert saved.status_code == 200, saved.text
    assert numbers(probe.json()["impact"]) == numbers(saved.json()["impact"]) == (150_000, 0, 0), (probe.json()["impact"], saved.json()["impact"])
    check(world, s, g, D(2026, 11, 12), charged=600_000, debt=150_000, advance=0, used=225_000, granted=225_000)


def _world_paid(world, fixed_today, *, forgiven=225_000, paid=225_000):
    """The pause 24.11 - 30.11 (November 450 000); 05.11 forgiven ``forgiven``; 10.11 paid ``paid``; the clock stands on 12.11."""
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    if forgiven:
        forgive(world, s, g, forgiven)
    fixed_today(D(2026, 11, 10))
    payment = pay(world, s, g, amount=paid, paid_on=D(2026, 11, 10))["payment"] if paid else None
    fixed_today(D(2026, 11, 12))
    return g, s, payment


def _row_numbers(body):
    return body["row"]["unpaid"], body["row"]["advance"]


def test_the_probe_of_a_correction_of_a_payment_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # November 450 000: forgiven 225 000 (05.11), paid 225 000 (10.11).  12.11 the payment is corrected to 300 000 (the moment of the FIRST entry stays: after the forgiveness): the forgiveness covers 225 000,
    # the money 225 000, the 75 000 left over are an advance.  Debt 0, advance 75 000, used 225 000 (the forgiveness is alive whole).
    g, s, payment = _world_paid(world, fixed_today)
    body = {"amount": 300_000, "paid_on": "2026-11-10", "method": "cash", "reason": "Неверная сумма"}
    url = f"{API}/org/payments/{payment['id']}/replace"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 201, probe.text
    saved = owner(world).post(url, json=body)
    assert saved.status_code == 201, saved.text
    assert _row_numbers(probe.json()) == _row_numbers(saved.json()) == (0, 75_000), (probe.json()["row"], saved.json()["row"])
    check(world, s, g, D(2026, 11, 12), charged=450_000, debt=0, advance=75_000, used=225_000, granted=225_000)


def test_the_probe_of_cancelling_a_payment_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # The same November; 12.11 the payment of 225 000 is CANCELLED: the forgiveness 225 000 covers half, the other half is a debt of 225 000; advance 0.
    g, s, payment = _world_paid(world, fixed_today)
    body = {"reason": "Деньги не пришли"}
    url = f"{API}/org/payments/{payment['id']}/void"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    saved = owner(world).post(url, json=body)
    assert saved.status_code == 200, saved.text
    assert _row_numbers(probe.json()) == _row_numbers(saved.json()) == (225_000, 0), (probe.json()["row"], saved.json()["row"])
    check(world, s, g, D(2026, 11, 12), charged=450_000, debt=225_000, advance=0, used=225_000, granted=225_000)


def test_the_probe_of_a_forgiveness_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # November 450 000, paid 225 000 (10.11), nothing forgiven yet.  12.11 the rest (225 000) is forgiven: unpaid 225 000 -> 0, advance 0, used 225 000.
    g, s, _ = _world_paid(world, fixed_today, forgiven=0)
    body = {"all": True, "reason": "Договорённость с родителем"}
    url = f"{API}/org/memberships/{membership(world, s, g).id}/writeoffs"
    probe = owner(world).post(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 201, probe.text
    saved = owner(world).post(url, json=body)
    assert saved.status_code == 201, saved.text
    assert (probe.json()["unpaid_before"], probe.json()["unpaid_after"]) == (saved.json()["unpaid_before"], saved.json()["unpaid_after"]) == (225_000, 0)
    assert _row_numbers(probe.json()) == _row_numbers(saved.json()) == (0, 0)
    check(world, s, g, D(2026, 11, 12), charged=450_000, debt=0, advance=0, used=225_000, granted=225_000)
