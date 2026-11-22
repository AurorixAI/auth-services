"""PAY-TAILS Б11.4 step 8.5 (lesson 164): «the probe = the record» for every route of a pause that answers a probe (``?dry_run=true``).  The probe answer, the answer of the save and the ledger read
afterwards say the SAME numbers (debt, advance, the change of the charge) - and the numbers are worked out by hand.  PostgreSQL, through the routes of the product.

World (ОМ, as ``test_pay_tails_b77_pg``): 600 000 a month, Tue / Thu / Fri, joined 02.11; November has 12 lessons, 50 000 each.  Pause 24.11 - 30.11 entered 03.11 (3 lessons lost: 450 000)."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_b77_pg import extend, pause_to_edit
from tests.money1b_helpers import add, fresh_student, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group
from tests.test_pay2_api_pg import settings_block
from tests.test_pay_tails_money_pg import TTF, check, forgive, led, om, refund, statement
from tests.ops_helpers import clock, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import client as payf_client
from tests.test_p154_money_pg import build_org, decide, extra_world, sheet

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


# ---------------------------------------------------------------------------------------------------------------------------- Б11.11 п. 2: the other routes with a probe
# Every probe is checked against the save and against the ledger read afterwards, in a world with a forgiveness and a pause that is entered AFTER the money (the order of the moments matters).


def _row_full(body):
    """What the card of the membership shows after the action: the unpaid sum, the advance, what can be returned, paid through."""
    row = body["row"]
    return row["unpaid"], row["advance"], row["refundable"], row["paid_until"]


def _effects(body):
    """The block «what it does to the saved sheets» of an answer without the flag of the probe and the sentence (it is the same in the future tense and in the past: «изменится» / «изменилась»)."""
    block = body.get("money_effects") or body.get("moneyEffects") or {}
    return {k: v for k, v in block.items() if k not in ("dryRun", "text")}


def _pause_at(world, fixed_today, s, g, day):
    """A pause 17.11 - 30.11 (November 600 000 -> 300 000) entered on ``day``."""
    fixed_today(day)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text


def _probe_and_save(world, url, body=None, *, method="post", **kw):
    call = getattr(owner(world), method)
    probe = call(url, params={"dry_run": "true"}, json=body or {}, **kw)
    saved = call(url, json=body or {}, **kw)
    assert probe.status_code == saved.status_code, (probe.text, saved.text)
    return probe.json(), saved.json()


def test_the_probe_of_restoring_a_payment_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # 05.11 forgiven 300 000; 10.11 paid 300 000; 12.11 a pause 17.11 - 30.11 (November 300 000): the money first - the forgiveness is cut to nothing.  13.11 the payment is cancelled: the forgiveness (alone)
    # covers the 300 000: debt 0, advance 0, used 300 000.  RESTORED (it keeps the moment of the first entry: before the pause): the forgiveness is cut again - debt 0, advance 0, used 0.
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 10))
    payment = pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 10))["payment"]
    _pause_at(world, fixed_today, s, g, D(2026, 11, 12))
    fixed_today(D(2026, 11, 13))
    assert owner(world).post(f"{API}/org/payments/{payment['id']}/void", json={"reason": "Деньги не пришли"}).status_code == 200
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=0, advance=0, used=300_000, granted=300_000)
    probe, saved = _probe_and_save(world, f"{API}/org/payments/{payment['id']}/restore", {"reason": "Деньги пришли"})
    assert _row_full(probe) == _row_full(saved) == (0, 0, 0, _row_full(saved)[3]), (probe["row"], saved["row"])
    assert _effects(probe) == _effects(saved)
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=0, advance=0, used=0, granted=300_000)


def test_the_probe_of_cancelling_a_forgiveness_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # 03.11 paid 150 000; 05.11 forgiven 150 000; 12.11 a pause (November 300 000): money 150 000 + forgiveness 150 000 = 300 000, debt 0, used 150 000.  13.11 the forgiveness is CANCELLED:
    # unpaid 150 000 (debt 150 000), used 0, granted 0.
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 5))
    wo = forgive(world, s, g, 150_000)["writeoff"]
    _pause_at(world, fixed_today, s, g, D(2026, 11, 12))
    fixed_today(D(2026, 11, 13))
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=0, advance=0, used=150_000, granted=150_000)
    probe, saved = _probe_and_save(world, f"{API}/org/writeoffs/{wo['id']}/void", {"reason": "Ошибка"})
    assert _row_full(probe) == _row_full(saved) and _row_full(saved)[:3] == (150_000, 0, 0), (probe["row"], saved["row"])
    assert _effects(probe) == _effects(saved)
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=150_000, advance=0, used=0, granted=0)


def test_the_probe_of_cancelling_a_refund_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # 03.11 paid 450 000; 05.11 forgiven 150 000; 08.11 a pause (November 300 000): money first - the forgiveness is cut off (used 0), the 150 000 of the money over the charge are an advance 150 000.
    # 12.11 that advance is returned (advance 0); 13.11 the refund is CANCELLED: advance 150 000, refundable 150 000, debt 0.
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=450_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 150_000)
    _pause_at(world, fixed_today, s, g, D(2026, 11, 8))
    fixed_today(D(2026, 11, 12))
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=150_000, used=0, granted=150_000)
    refunded = refund(world, s, g, 150_000, expect=201).json()["refund"]
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=0, granted=150_000)
    fixed_today(D(2026, 11, 13))
    probe, saved = _probe_and_save(world, f"{API}/org/refunds/{refunded['id']}/void", {"reason": "Деньги вернули обратно"})
    assert _row_full(probe) == _row_full(saved) and _row_full(saved)[:3] == (0, 150_000, 150_000), (probe["row"], saved["row"])
    assert _effects(probe) == _effects(saved)
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=0, advance=150_000, used=0, granted=150_000)


def _since_september(world, fixed_today):
    """The student joined 01.09 (the accounting started 01.09 then); on 03.11 the owner moved the start of the accounting to 01.10: the clock stands on 03.11, September is not charged."""
    fixed_today(D(2026, 9, 1))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(world.db, world.org, world.teacher, "Общий")
    give_group_price(world.db, g, 600_000)
    set_schedule(world, TTF, g)
    ensure_billing(world, g, price=None)
    s = fresh_student(world, "Ученик с сентября")
    add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 3))
    moved = owner(world).put(f"{API}/org/settings/payments", json={"accounting_start_date": "2026-10-01"})
    assert moved.status_code == 200, moved.text
    return g, s


def test_the_probe_of_the_start_of_the_accounting_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # The student has stayed since 01.09; the accounting starts 01.10.  05.10 (entered 03.11) 600 000 paid; 03.11 300 000 paid and 300 000 forgiven (in this order); 12.11 a pause (November 300 000):
    # October 600 000 / money 600 000, November 300 000 / money 300 000, the forgiveness (entered after the money, 300 000 over the room) is cut: charged 900 000, debt 0, advance 0, used 0, granted 300 000.
    # The accounting starts 01.09: September 600 000 comes FIRST and takes the first payment (600 000); October gets the second payment 300 000 + the forgiveness 300 000 (a forgiveness lies where it finds
    # room); November (300 000) gets nothing: charged 1 500 000, debt 300 000, advance 0, used 300 000.  Debt +300 000 for 1 membership.
    g, s = _since_september(world, fixed_today)
    pay(world, s, g, amount=600_000, paid_on=D(2026, 10, 5))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    forgive(world, s, g, 300_000)
    _pause_at(world, fixed_today, s, g, D(2026, 11, 12))
    check(world, s, g, D(2026, 11, 12), charged=900_000, debt=0, advance=0, used=0, granted=300_000)
    url = f"{API}/org/settings/payments"
    probe = owner(world).put(url, params={"dry_run": "true"}, json={"accounting_start_date": "2026-09-01"})
    assert probe.status_code == 200, probe.text
    saved = owner(world).put(url, json={"accounting_start_date": "2026-09-01"})
    assert saved.status_code == 200, saved.text
    impact = probe.json()["impact"]
    assert (impact["memberships_with_new_debt"], impact["debt_delta"], impact["memberships_with_changed_advance"], impact["advance_delta"]) == (1, 300_000, 0, 0), impact
    check(world, s, g, D(2026, 11, 12), charged=1_500_000, debt=300_000, advance=0, used=300_000, granted=300_000)


def test_the_probe_of_the_term_of_payment_the_save_and_the_statement_after_say_the_same(world, fixed_today):
    # 05.11 forgiven 100 000; 08.11 a pause 17.11 - 30.11 (November 300 000): unpaid 200 000 on 12.11.  The term 5 days (the 5th day of November, 07.11, is past): a DEBT 200 000.  The term 14 days
    # (the 14th day without a pause is 16.11): «К оплате» 200 000.  Probe: 1 student goes from the debtors to «К оплате»; after the save: debt_amount 0, due_amount 200 000.
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 100_000)
    _pause_at(world, fixed_today, s, g, D(2026, 11, 8))
    fixed_today(D(2026, 11, 12))
    sm = statement(world, s, g)["summary"]
    assert (sm["debt_amount"], sm["due_amount"]) == (200_000, 0), sm
    url = f"{API}/org/settings/payments"
    probe = owner(world).put(url, params={"dry_run": "true"}, json={"pay_due_days": 14})
    assert probe.status_code == 200, probe.text
    assert probe.json()["pay_due_impact"]["to_due"] == 1 and probe.json()["pay_due_impact"]["to_debt"] == 0, probe.json()["pay_due_impact"]
    assert owner(world).put(url, json={"pay_due_days": 14}).status_code == 200
    sm = statement(world, s, g)["summary"]
    assert (sm["debt_amount"], sm["due_amount"]) == (0, 200_000), sm
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=200_000, advance=0, used=100_000, granted=100_000)       # a name only: the sum is the same


def test_the_probe_of_an_opening_balance_the_save_and_the_ledger_after_say_the_same(world, fixed_today):
    # 05.11 forgiven 300 000; 10.11 paid 300 000; 12.11 a pause (November 300 000): the forgiveness is cut off, debt 0, advance 0, used 0.  13.11 an opening debt 400 000 (on 31.10) is entered: it is the FIRST
    # place of the account and takes the older forgiveness (300 000) and 100 000 of the money; November (300 000) gets the other 200 000 of the money: debt 100 000, advance 0, used 300 000, charged 700 000.
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 10))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 10))
    _pause_at(world, fixed_today, s, g, D(2026, 11, 12))
    fixed_today(D(2026, 11, 13))
    check(world, s, g, D(2026, 11, 13), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    url = f"{API}/org/memberships/{membership(world, s, g).id}/opening-balance"
    body = {"debt_amount": 400_000, "debt_on": "2026-10-31", "reason": "Долг из тетради"}
    probe, saved = _probe_and_save(world, url, body, method="put")
    nums = lambda b: (b["impact"]["debt_after"], b["impact"]["advance_after"], b["impact"]["charged_delta"], b["row"]["unpaid"], b["row"]["advance"])
    assert nums(probe) == nums(saved) == (100_000, 0, 400_000, 100_000, 0), (probe["impact"], saved["impact"])
    check(world, s, g, D(2026, 11, 13), charged=700_000, debt=100_000, advance=0, used=300_000, granted=300_000)


def _saved_sheet(w, month="2026-10"):
    """The owner saves the sheet of the teacher as it stands (the probe of an action must say what it does to THIS sheet)."""
    s = sheet(w, month)
    r = payf_client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": month, "teacher_id": str(w.A.id), "expected_fingerprint": s["source_fingerprint"]})
    assert r.status_code == 201, r.text
    return s["total_amount"], r.json()["id"]


def _line(effects):
    (line,) = effects["payroll"]
    return line["freshBefore"], line["freshAfter"], line["changedBy"]


def test_the_probe_of_a_decision_on_an_additional_lesson_the_save_and_the_sheet_after_say_the_same(pg_session, clock):
    # October, 5 students paid, an additional lesson (3 came, d absent, e excused-pending) waits for the decision: the saved sheet says 0.  «Оплатить» pays for all 5: 5 x 25 000 = 125 000.
    w = build_org(pg_session)
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    total, sheet_id = _saved_sheet(w)
    assert total == 0
    url = f"{API}/org/one-off-lessons/{made}/money-decision"
    probe = payf_client(w, "owner").post(url, params={"dry_run": "true"}, json={"decision": "pay"})
    assert probe.status_code == 200, probe.text
    saved = payf_client(w, "owner").post(url, json={"decision": "pay"})
    assert saved.status_code == 200, saved.text
    assert probe.json()["dryRun"] is True and saved.json()["dryRun"] is False
    assert _effects(probe.json()) == _effects(saved.json()) and _line(_effects(saved.json())) == (0, 125_000, 125_000), (probe.json(), saved.json())
    assert sheet(w, "2026-10")["total_amount"] == 125_000


def _excuse_of(w, g, made, student):
    """The pending request of the student for the lesson (the way ``test_money1_extra_decision_pg`` puts it: the row of the request that the parent's message leaves)."""
    from src.models.auth import AttendanceExcuse, GroupAttendance

    att = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id=made).one()
    row = w.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=student.id).first()
    if row is None:
        row = AttendanceExcuse(org_id=w.org.id, attendance_id=att.id, student_id=student.id, status="pending", reason="Справка", reason_by=w.owner.id, reason_at=att.created_at)
        w.db.add(row)
        w.db.commit()
    return row


def test_the_probe_of_confirming_an_excuse_the_save_and_the_sheet_after_say_the_same(pg_session, clock):
    # The same lesson, «Оплатить» was decided: 5 x 25 000 = 125 000 on the saved sheet.  The excuse of e is CONFIRMED: the lesson is not paid for him (the rule of the excuse): 4 x 25 000 = 100 000, -25 000.
    w = build_org(pg_session)
    g, (a, b, c, d, e), ms, made = extra_world(w, clock, "wait", "A")
    decide(w, "owner", made, "pay")
    total, sheet_id = _saved_sheet(w)
    assert total == 125_000
    excuse = _excuse_of(w, g, made, e)
    url = f"{API}/org/attendance/excuses/{excuse.id}/review"
    probe = payf_client(w, "owner").post(url, params={"dry_run": "true"}, json={"decision": "confirmed"})
    assert probe.status_code == 200, probe.text
    saved = payf_client(w, "owner").post(url, json={"decision": "confirmed"})
    assert saved.status_code == 200, saved.text
    assert _effects(probe.json()) == _effects(saved.json()) and _line(_effects(saved.json())) == (125_000, 100_000, -25_000), (probe.json(), saved.json())
    assert sheet(w, "2026-10")["total_amount"] == 100_000


def test_the_probe_of_a_type_of_lessons_the_save_and_the_sheet_after_say_the_same(pg_session, clock):
    # The additional lesson is paid (125 000 on the saved sheet of October, the rate of «group» 50 %).  The rate of «pair» is 100 %; the type of the group becomes «pair» since 01.10: the pay of the month
    # moves 125 000 -> 250 000 and the saved sheet turns stale.  The probe says 1 sheet (October); after the save the saved sheet is stale (it still says 125 000) and the fresh total is 250 000.
    from decimal import Decimal

    from src.models.auth import PayrollRate

    w = build_org(pg_session)
    w.db.add(PayrollRate(org_id=w.org.id, teacher_id=w.A.id, lesson_type="pair", percent=Decimal("100"), valid_from=D(2020, 1, 1), created_by=w.owner.id))
    w.db.commit()
    g, st, ms, made = extra_world(w, clock, "wait", "A")
    decide(w, "owner", made, "pay")
    total, sheet_id = _saved_sheet(w)
    assert total == 125_000
    url = f"{API}/org/groups/{g.id}/lesson-type"
    body = {"lesson_type": "pair", "valid_from": "2026-10-01"}
    probe = payf_client(w, "owner").put(url, params={"dry_run": "true"}, json=body)
    assert probe.status_code == 200, probe.text
    saved = payf_client(w, "owner").put(url, json=body)
    assert saved.status_code == 200, saved.text
    said = probe.json()["impact"]
    assert said["stale_payroll_sheets"] == 1 and [x["month"] for x in said["stale_sheets"]] == ["2026-10"], said
    got = payf_client(w, "owner").get(f"{API}/org/payroll/sheets/{sheet_id}").json()
    assert got["stale"] is True and got["total_amount"] == 125_000, got.get("stale")
    assert sheet(w, "2026-10")["total_amount"] == 250_000


# ---------------------------------------------------------------------------------------------------------------------------- Б11.11 п. 3: a probe leaves nothing behind (step 8.5 writes the row of the journal in a probe and relies on the rollback)


def _rows(w):
    """The rows a write leaves: the journal (``GroupAudit``), the pauses (voided ones too), the payments (voided ones too)."""
    from src.models.auth import GroupAudit, MembershipPause, StudentPayment

    w.db.expire_all()
    return {"journal": w.db.query(GroupAudit).count(), "pauses": w.db.query(MembershipPause).count(), "payments": w.db.query(StudentPayment).count()}


def test_a_probe_of_a_pause_and_of_a_payment_leaves_no_row_of_the_journal_no_pause_and_no_payment(world, fixed_today):
    # A pause 24.11 - 30.11 (entered 03.11), 05.11 forgiven 225 000, 10.11 paid 225 000.  On 12.11 five probes - an edit of the pause, a new pause, taking a pause off, the correction of the payment and its
    # cancel - change nothing in the base: the same number of rows of the journal, of pauses and of payments.  (The same calls SAVED do write: the check is not empty.)
    g, s, pause_id = pause_to_edit(world, fixed_today)
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 225_000)
    fixed_today(D(2026, 11, 10))
    payment = pay(world, s, g, amount=225_000, paid_on=D(2026, 11, 10))["payment"]
    fixed_today(D(2026, 11, 12))
    mid = membership(world, s, g).id
    before = _rows(world)
    probes = [
        ("patch", f"{API}/org/pauses/{pause_id}", {"first_day": "2026-11-17", "reason": "Болезнь продлилась"}),
        ("post", f"{API}/org/memberships/{mid}/pauses", {"first_day": "2026-11-16", "last_day": "2026-11-20", "reason": "Ещё одна"}),
        ("post", f"{API}/org/pauses/{pause_id}/void", {"reason": "Поставили по ошибке"}),
        ("post", f"{API}/org/payments/{payment['id']}/replace", {"amount": 300_000, "paid_on": "2026-11-10", "method": "cash", "reason": "Неверная сумма"}),
        ("post", f"{API}/org/payments/{payment['id']}/void", {"reason": "Деньги не пришли"}),
    ]
    for method, url, body in probes:
        r = getattr(owner(world), method)(url, params={"dry_run": "true"}, json=body)
        assert r.status_code in (200, 201), (url, r.text)
        assert _rows(world) == before, (url, before, _rows(world))
    r = owner(world).post(probes[1][1], json=probes[1][2])                          # the same call saved: a pause and a row of the journal appear
    assert r.status_code == 201, r.text
    after = _rows(world)
    assert (after["pauses"], after["journal"]) == (before["pauses"] + 1, before["journal"] + 1) and after["payments"] == before["payments"], (before, after)
