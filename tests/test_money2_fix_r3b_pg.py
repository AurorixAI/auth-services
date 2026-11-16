"""Money-2, the second round after the independent check (R3b, 17.11): В1-В4 and the small things (PostgreSQL).  The numbers of every money test are worked by hand in its comment."""
from datetime import date, timedelta

import pytest

from tests.money1b_helpers import add, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_fix_r3_pg import TUE_THU, account, left_with_debt_100, leave, live_sum, periods_billing_from, replace_it, restore_it, void_it
from tests.test_money2_transfer_a_pg import groups3, snap
from tests.test_money2_fix_r3_pg import admin_client, world_p4
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)

pytestmark = needs_pg

FRIDAY = date(2026, 11, 27)
FIRST_LESSON = date(2026, 12, 1)          # the next Tuesday after Friday 27.11


# ============================================================================================================ В1: a cancelled leaving keeps the «Оплата с» that has not begun


@pytest.mark.parametrize("settings", [
    {},                                                                               # «Общий месяц»
    {"billing_anchor": "payment_date"},                                               # the personal period
    {"period_mode": "lessons", "lessons_per_payment": 8},                             # a package centre
], ids=["shared_month", "personal_period", "package"])
@pytest.mark.parametrize("explicit", [False, True], ids=["automatic_pay_from", "explicit_pay_from"])
def test_v1_leaving_and_coming_back_the_same_day_keeps_a_pay_from_that_has_not_begun(world, fixed_today, settings, explicit):
    # a group of Tuesdays and Thursdays (B), the student of A is written into B on Friday 27.11: no lesson that day, the first one is Tuesday 01.12 (automatic) or the owner chose Thursday 03.12 (explicit).
    # In the first month nothing is charged before «Оплата с»; he leaves B and comes back the same day: the stay is the SAME stay, so «Оплата с» stays 01.12 (03.12), not 27.11.
    # Hand count (package centre, 8 lessons for 300 000 -> 37 500 a lesson... the numbers of the ledger are compared before/after: they must be equal, and the period's own «Оплата с» is the exact check).
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, b)
    if settings:
        settings_block(world, **settings)
    s0 = world.students[0]
    day = date(2026, 12, 3) if explicit else FIRST_LESSON
    out = add(world, s0, group=b, mode="add", **({"pay_from": day.isoformat()} if explicit else {}))
    assert out["billing"]["pay_from"] == day.isoformat() if out["billing"] and "pay_from" in out["billing"] else True
    before = snap(world, s0, b)
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [day]
    leave(world, b, s0)
    r = add(world, s0, group=b, mode="add")
    assert r["leave_undo"]["undone"] is True
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [day, day]          # the closed period keeps its own, the new one carries it on
    assert snap(world, s0, b) == before


def test_v1_a_pay_from_that_has_already_begun_is_not_carried(world, fixed_today):
    # «Оплата с» 02.11 is in the past on Friday 27.11: the return the same day is the same stay (nothing to carry), the new period has no own «Оплата с»
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, b)
    s0 = world.students[0]
    add(world, s0, group=b, mode="add", pay_from="2026-11-02")
    leave(world, b, s0)
    assert add(world, s0, group=b, mode="add")["leave_undo"]["undone"] is True
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [date(2026, 11, 2), None]


# ============================================================================================================ В2: the ceiling counts the WHOLE chain of versions


def change_method(w, payment, method, expect=201):
    r = owner(w).post(f"{API}/org/payments/{payment['id']}/replace", json={"amount": payment["amount"], "paid_on": payment["paid_on"], "method": method, "reason": "Исправление способа"})
    assert r.status_code == expect, r.text
    return r.json()


def test_v2_a_correction_through_a_method_only_version_does_not_bring_the_old_biggest_sum_back(world, fixed_today):
    # debt 100 000 (see left_with_debt_100: 210 000 charged, 110 000 paid).  P1 = 100 000 -> corrected to 10 000 (P1') -> P2 = 90 000 (debt 0) -> P1' gets only another method (P1'', 10 000, no new sum)
    # -> P1'' to 100 000: P2 is a live payment OUTSIDE the chain taken after P1 was replaced, so 100 000 is not «not new money».  Ceiling = max(debt without P1'' = 100 000 - 90 000 = 10 000, own 10 000) = 10 000.
    # Before the fix the new version took the creation time of P1'': nothing after it -> the ceiling was the biggest of the chain, 100 000, and 110 000 + 100 000 + 90 000 = 300 000 was paid.
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    cut = replace_it(world, p1, 10_000)["payment"]
    pay(world, s0, a, amount=90_000)
    same = change_method(world, cut, "card")["payment"]
    d = replace_it(world, same, 100_000, expect=422)["detail"]
    assert d["code"] == "PAYMENT_EXCEEDS_DEBT" and d["debt"] == 10_000
    assert live_sum(world, a, s0) == 110_000 + 10_000 + 90_000 and account(world, membership(world, s0, a).id).advance == 0
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s0, a).id}/payments/preview", params={"amount": 100_000, "replace_payment_id": same["id"]})
    assert r.status_code == 200 and r.json()["suggested_amount"] == 10_000


def test_v2_a_correction_through_a_cancelled_and_restored_version_does_not_bring_the_old_biggest_sum_back(world, fixed_today):
    # the same chain, but P1' is cancelled and restored (P1'' stands in the same chain through replaces_payment_id), then P1'' -> 100 000: refused, the sum of all stays 300 000 - 100 000 + 10 000... = 210 000
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    cut = replace_it(world, p1, 10_000)["payment"]
    pay(world, s0, a, amount=90_000)
    void_it(world, cut)
    back = restore_it(world, cut)["payment"]
    d = replace_it(world, back, 100_000, expect=422)["detail"]
    assert d["code"] == "PAYMENT_EXCEEDS_DEBT" and d["debt"] == 10_000
    assert live_sum(world, a, s0) == 110_000 + 10_000 + 90_000 and account(world, membership(world, s0, a).id).advance == 0


def test_v2_the_honest_cases_of_the_chain_stay(world, fixed_today):
    # (1) 600 -> 200 -> 600 (no other payment): the biggest of the chain is allowed again
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    cut = replace_it(world, p1, 20_000)["payment"]
    replace_it(world, cut, 100_000)
    assert live_sum(world, a, s0) == 210_000
    # (2) cancel -> restore with nothing between, then a correction back up through a method-only version: the chain is clean
    a, s1 = left_with_debt_100(world, fixed_today, index=1)
    q1 = pay(world, s1, a, amount=100_000)["payment"]
    cut = replace_it(world, q1, 10_000)["payment"]
    only = change_method(world, cut, "card")["payment"]
    replace_it(world, only, 100_000)
    assert live_sum(world, a, s1) == 210_000
    # (3) cancel -> restore with nothing between: the same payment comes back
    s2 = world.students[2]
    from tests.test_money2_transfer_a_pg import joined

    joined(world, s2, a, date(2026, 11, 2))
    pay(world, s2, a, amount=110_000, paid_on=date(2026, 11, 2))
    leave(world, a, s2)
    r1 = pay(world, s2, a, amount=100_000)["payment"]
    void_it(world, r1)
    back = restore_it(world, r1)["payment"]
    replace_it(world, back, 100_000)
    assert live_sum(world, a, s2) == 210_000


def test_v2_restoring_a_cancelled_version_after_a_payment_outside_its_chain_is_refused(world, fixed_today):
    # P1 = 100 000, P1' = 10 000 (P1 replaced), P2 = 90 000 (another payment), then P1' is cancelled and P3 = 10 000 is taken (debt 0 again): restoring P1' brings 110 000 + 90 000 + 10 000 + 10 000 = 220 000 > 210 000.
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    cut = replace_it(world, p1, 10_000)["payment"]
    pay(world, s0, a, amount=90_000)
    void_it(world, cut)
    pay(world, s0, a, amount=10_000)
    assert restore_it(world, cut, expect=409)["detail"]["code"] == "PAYMENT_RESTORE_SUPERSEDED"
    assert live_sum(world, a, s0) == 110_000 + 90_000 + 10_000


# ============================================================================================================ В3: an administrator changes NOTHING of an old record but the note; the owner's cancellation is the owner's to take back


def change_method_as(client, payment, method, note=None, expect=201):
    body = {"amount": payment["amount"], "paid_on": payment["paid_on"], "method": method, "reason": "Исправление"}
    if note is not None:
        body["note"] = note
    r = client.post(f"{API}/org/payments/{payment['id']}/replace", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def test_v3_an_administrator_cannot_change_even_the_method_of_a_record_older_than_seven_days_only_the_note(world, fixed_today):
    # today 20.11: old = 02.11 (18 days), e8 = 12.11 (8 days), e7 = 13.11 (7 days).  A change of the method cash -> card moves the expected sum of the till of TODAY, so it is money too.
    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    admin = admin_client(world)
    assert old["method"] == "cash"
    for rec in (old, e8):
        d = change_method_as(admin, rec, "card", expect=403)["detail"]
        assert d["code"] == "BACKDATE_OWNER_ONLY" and d["message"] == "Запись старше 7 дней меняет владелец"
    assert admin.post(f"{API}/org/payments/{old['id']}/replace", params={"dry_run": "true"}, json={"amount": 300_000, "paid_on": old["paid_on"], "method": "card", "reason": "Проба"}).status_code == 403
    change_method_as(admin, old, "cash", note="Квитанция № 5")                                 # the note alone: allowed
    change_method_as(admin, e7, "card")                                                         # 7 days: the method as before
    change_method_as(owner(world), e8, "card")                                                  # the owner: any record
    assert live_sum(world, a, s0) == 320_000


def make_voided_by(world, fixed_today, who):
    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    client = owner(world) if who == "owner" else admin_client(world)
    void_it(world, e7, client=client)
    return a, s0, e7


def payments_of(client, membership_id):
    r = client.get(f"{API}/org/memberships/{membership_id}/payments")
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()}


def test_v3_a_payment_cancelled_by_the_owner_is_restored_only_by_the_owner(world, fixed_today):
    a, s0, e7 = make_voided_by(world, fixed_today, "owner")
    admin = admin_client(world)
    m = membership(world, s0, a).id
    assert payments_of(admin, m)[e7["id"]]["restorable"] is False and payments_of(owner(world), m)[e7["id"]]["restorable"] is True
    r = admin.post(f"{API}/org/payments/{e7['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "RESTORE_OWNER_ONLY" and r.json()["detail"]["message"] == "Оплату отменил владелец — восстановить может только он"
    assert admin.post(f"{API}/org/payments/{e7['id']}/restore", params={"dry_run": "true"}, json={"reason": "Отменили по ошибке"}).status_code == 403
    assert live_sum(world, a, s0) == 310_000                                                    # nothing came back
    restore_it(world, e7)                                                                       # the owner
    assert live_sum(world, a, s0) == 320_000


def test_v3_a_payment_cancelled_by_an_administrator_is_restored_by_either(world, fixed_today):
    a, s0, e7 = make_voided_by(world, fixed_today, "admin")
    admin = admin_client(world)
    assert payments_of(admin, membership(world, s0, a).id)[e7["id"]]["restorable"] is True
    r = admin.post(f"{API}/org/payments/{e7['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == 201, r.text
    assert live_sum(world, a, s0) == 320_000


def test_small_the_restorable_flag_costs_the_same_number_of_queries_for_two_and_for_twelve_cancelled_payments(world, fixed_today):
    from sqlalchemy import event

    from src.auth.payments_service import PaymentService

    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    m = membership(world, s0, a).id

    def count():
        n = []
        engine = world.db.get_bind()
        listener = lambda *args: n.append(1)       # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            PaymentService(world.db).list_payments(str(world.org.id), m)
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(n)

    for p in (e8, e7):
        void_it(world, p)
    few = count()
    for _ in range(10):
        void_it(world, pay(world, s0, a, amount=1_000, paid_on=date(2026, 11, 19))["payment"])
    assert count() == few
