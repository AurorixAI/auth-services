"""Money-1 pack V (PostgreSQL): operations on the money of a student - the amount a person SAW and a repeated request (V1).

Numbers of ``test_pay2_api_pg`` worlds: ``e6_world`` debt 100 000 (price 500 000, 400 000 paid), ``e7`` an advance of 344 000 (790 000 paid, left on 25.10)."""
from uuid import uuid4

from src.models.auth import GroupAudit, StudentPayment, StudentRefund, StudentWriteoff

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401  (fixtures)
    API, D, TODAY, e1_world, e6_world, fixed_today, membership, owner, post_payment, seed_payment, stay,
)

pytestmark = needs_pg
NB = " "


def key():
    return {"Idempotency-Key": str(uuid4())}


def writeoffs(w):
    w.db.expire_all()
    return w.db.query(StudentWriteoff).filter_by(org_id=w.org.id).all()


def refunds(w):
    w.db.expire_all()
    return w.db.query(StudentRefund).filter_by(org_id=w.org.id).all()


# ---- V1: the sum the person saw -------------------------------------------------------------------------------------------------------------


def test_v1_a_whole_forgiveness_of_a_debt_that_moved_is_refused_and_nothing_is_written(world, fixed_today):
    m = e6_world(world)                                                        # debt 100 000
    post_payment(world, m, amount=30_000)                                       # while the window was open another person took 30 000
    r = owner(world).post(f"{API}/org/memberships/{m.id}/writeoffs", json={"all": True, "reason": "льгота", "expected_debt": 100_000})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "BALANCE_CHANGED" and d["debt"] == 70_000
    assert d["message"] == f"Долг изменился: было 100{NB}000{NB}сум, сейчас 70{NB}000{NB}сум"
    assert writeoffs(world) == []                                               # the old code wrote 70 000


def test_v1_the_same_sum_is_written_and_a_request_without_the_sum_works_as_before(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    assert c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 10_000, "reason": "льгота", "expected_debt": 100_000}).status_code == 201
    assert c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 10_000, "reason": "льгота"}).status_code == 201       # no sum: the old behaviour
    assert [w.amount for w in writeoffs(world)] == [10_000, 10_000]


def test_v1_a_double_click_with_one_key_writes_one_forgiveness(world, fixed_today):
    m = e6_world(world)
    c, h = owner(world), key()
    first = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=h)
    again = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=h)
    assert first.status_code == 201 and again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "DUPLICATE_WRITEOFF" and again.json()["detail"]["writeoff_id"] == first.json()["writeoff"]["id"]
    assert [w.amount for w in writeoffs(world)] == [20_000]                      # the old code: two rows, 40 000
    other = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=key())       # a new key is a new decision
    assert other.status_code == 201 and len(writeoffs(world)) == 2


def test_v1_voiding_a_forgiveness_checks_the_debt_the_person_saw_and_a_second_void_says_who_did_it(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    wid = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 40_000, "reason": "льгота"}).json()["writeoff"]["id"]      # debt 60 000
    stale = c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ошибка", "expected_debt": 100_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["debt"] == 60_000
    assert c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ошибка", "expected_debt": 60_000}).status_code == 200
    second = c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ещё раз"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "ALREADY_VOIDED" and second.json()["detail"]["voided_at"]
    assert world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "payment_writeoff_void").count() == 1


def _advance_world(w, fixed_today):
    fixed_today(D(2026, 10, 25))
    m = e1_world(w)
    seed_payment(w, m, 790_000, TODAY, receipt_no=18)
    stay(w, m, D(2026, 9, 15), D(2026, 10, 25))
    return m                                                                    # refundable 344 000


def test_v1_a_refund_of_an_advance_that_moved_is_refused_and_a_double_click_writes_one(world, fixed_today):
    m = _advance_world(world, fixed_today)
    c, h = owner(world), key()
    stale = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "ученик ушёл", "expected_refundable": 300_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["refundable"] == 344_000
    assert refunds(world) == []
    body = {"amount": 100_000, "method": "cash", "reason": "ученик ушёл", "expected_refundable": 344_000}
    assert c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=h).status_code == 201
    again = c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=h)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "DUPLICATE_REFUND" and again.json()["detail"]["refund_id"]
    assert [r.amount for r in refunds(world)] == [100_000]                       # the old code: two rows
    moved = c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=key())      # the first refund moved the advance: 344 000 -> 244 000
    assert moved.status_code == 409 and moved.json()["detail"]["refundable"] == 244_000 and len(refunds(world)) == 1


def test_v1_voiding_a_refund_checks_the_advance_and_names_the_voider_twice(world, fixed_today):
    m = _advance_world(world, fixed_today)
    c = owner(world)
    rid = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "ушёл"}).json()["refund"]["id"]
    stale = c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ошибка", "expected_advance": 344_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["advance"] == 244_000
    assert c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ошибка", "expected_advance": 244_000}).status_code == 200
    second = c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ещё раз"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "ALREADY_VOIDED" and second.json()["detail"]["voided_at"]


def test_v1_a_forgiveness_in_a_closed_month_is_allowed_and_the_key_of_another_centre_does_not_interfere(world, fixed_today):
    m = e6_world(world)
    h = key()
    assert owner(world).post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 5_000, "reason": "льгота"}, headers=h).status_code == 201
    assert len(writeoffs(world)) == 1                                           # ДП-07: money of a student is not blocked by a closed month of a teacher (the answer warns)


# ---- V2: the journal of money decisions ---------------------------------------------------------------------------------------------------


def journal(w, first, last, **params):
    r = owner(w).get(f"{API}/org/payments/journal", params={"from": first, "to": last, **params})
    assert r.status_code == 200, r.text
    return r.json()


def audit(w, action):
    w.db.expire_all()
    rows = w.db.query(GroupAudit).filter(GroupAudit.org_id == w.org.id, GroupAudit.action == action).all()
    assert len(rows) == 1, (action, len(rows))
    return rows[0].snapshot


def test_v2_a_payment_cancelled_in_november_is_a_line_of_november_too_and_the_totals_do_not_double(world, fixed_today):
    fixed_today(D(2026, 11, 12))
    m = e6_world(world)                                                           # 400 000 paid on 15.10 for the price 500 000: debt 100 000
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    assert owner(world).post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошиблись суммой"}).status_code == 200
    october = journal(world, "2026-10-01", "2026-10-31")
    assert october["total"] == 1 and october["rows"][0]["status"] == "voided" and october["rows"][0]["view"] == "paid" and october["totals"]["sum"] == 0
    assert journal(world, "2026-11-01", "2026-11-30")["total"] == 0                 # as before: the month of the cancellation did not show it
    november = journal(world, "2026-11-01", "2026-11-30", cancelled_in_period="true")
    assert november["total"] == 1 and november["rows"][0]["view"] == "cancelled_here" and november["rows"][0]["amount"] == 400_000
    assert (november["totals"]["count"], november["totals"]["sum"]) == (0, 0)       # the sum of the month is the money that stays
    assert (november["totals"]["cancelled_here_count"], november["totals"]["cancelled_here_sum"]) == (1, 400_000)
    snap = audit(world, "payment_void")
    assert (snap["amount"], snap["reason"], snap["debt_before"], snap["debt_after"]) == (400_000, "ошиблись суммой", 100_000, 500_000)


def test_v2_a_corrected_payment_leaves_the_old_and_the_new_sum_and_the_debt_before_and_after(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    r = owner(world).post(f"{API}/org/payments/{pay.id}/replace", json={"amount": 350_000, "paid_on": TODAY.isoformat(), "method": "cash", "reason": "неверная сумма"})
    assert r.status_code == 201, r.text
    snap = audit(world, "payment_replace")
    assert (snap["amount"], snap["new_amount"], snap["debt_before"], snap["debt_after"]) == (400_000, 350_000, 100_000, 150_000)
    csv = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv"}).text
    csv_all = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv", "with_replaced": "true"}).text
    assert "Исправлена (заменена новой)" not in csv and "Исправлена (заменена новой)" in csv_all           # the old file is not changed; the replaced version only when asked for


def test_v2_the_list_of_decisions_has_author_reason_and_debt_before_and_after_and_is_closed_to_others(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 40_000, "reason": "болезнь"})
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c.post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошибка ввода"})
    got = c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert got.status_code == 200, got.text
    rows = got.json()["rows"]
    assert [x["kind"] for x in rows] == ["payment_void", "payment_writeoff"] and got.json()["total"] == 2
    assert (rows[1]["amount"], rows[1]["reason"], rows[1]["debt_before"], rows[1]["debt_after"]) == (40_000, "болезнь", 100_000, 60_000)
    assert rows[0]["actor_name"] and rows[0]["student_name"] and rows[0]["kind_label"] == "Оплата отменена"
    only = c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31", "kind": "payment_writeoff"}).json()
    assert only["total"] == 1
    from tests.pg_world import make_client
    assert make_client(world, user=world.teacher).get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"}).status_code == 403


# ---- V3: a reasonable price and the correction of a typo -------------------------------------------------------------------------------------


def put_price(w, amount, expect=200, **body):
    r = owner(w).put(f"{API}/org/groups/{w.group.id}/price", params={"dry_run": "true"} if body.pop("dry", False) else {}, json={"amount": amount, **body})
    assert r.status_code == expect, r.text
    return r.json()


def test_v3_a_price_in_times_needs_a_confirmation_the_window_warns_and_the_row_is_not_written_without_it(world, fixed_today):
    e6_world(world)                                                                # the price of the group: 500 000
    refused = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 5_000_000})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "PRICE_UNUSUAL"
    assert refused.json()["detail"]["message"] == f"Вы уверены: 5{NB}000{NB}000{NB}сум? Сейчас 500{NB}000{NB}сум"
    assert owner(world).get(f"{API}/org/groups/{world.group.id}/prices").json()["current"]["amount"] == 500_000          # the old code wrote 5 000 000
    assert put_price(world, 5_000_000, dry=True)["impact"]["warnings"][0]["code"] == "price_unusual"                   # the window asks BEFORE it saves
    assert put_price(world, 5_000_000, confirm_unusual=True)["current"]["amount"] == 5_000_000


def test_v3_the_borders_of_the_sensible_price_are_twice_and_a_half_and_zero(world, fixed_today):
    e6_world(world)                                                                # 500 000
    for amount, warned in ((1_000_000, False), (1_000_001, True), (250_000, False), (249_999, True), (0, True), (500_000, False)):
        got = put_price(world, amount, dry=True)["impact"]["warnings"]
        assert bool(got) is warned, (amount, got)


def typo_world(w):
    """e6 (price 500 000 from 01.01, 400 000 paid on 15.10) + a typo of the SAME day: 5 000 000 from 01.10 - the student owes 4 600 000 for the period of 15.10."""
    from tests.pg_world import give_group_price

    from datetime import datetime

    from src.models.auth import GroupPrice

    m = e6_world(w)
    give_group_price(w.db, w.group, 5_000_000, valid_from=D(2026, 10, 1))
    w.db.query(GroupPrice).filter_by(group_id=w.group.id, valid_from=D(2026, 10, 1)).update({"created_at": datetime(2026, 10, 2, 8, 0)})      # a row of the past: not «written while the request waited»
    w.db.commit()
    return m


def test_v3_a_typo_in_the_price_is_corrected_backdated_with_a_reason_and_the_window_says_it_can(world, fixed_today):
    m = typo_world(world)
    blocked = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 500_000, "valid_from": "2026-10-01", "confirm_unusual": True})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "PRICE_BACKDATE_WITH_PAYMENTS" and blocked.json()["detail"]["can_correct"] is True
    no_reason = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 500_000, "valid_from": "2026-10-01", "correction": True, "confirm_unusual": True})
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "REASON_REQUIRED"
    done = put_price(world, 500_000, valid_from="2026-10-01", correction=True, correction_reason="опечатка в цене", confirm_unusual=True)
    assert done["current"]["amount"] == 500_000
    paid_one = [i for i in done["impact"]["money"]["items"] if i["has_money"]][0]                   # the student with the payment: 4 600 000 -> 100 000
    assert (paid_one["debt_before"], paid_one["debt_after"]) == (4_600_000, 100_000)
    row = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert (row["debt"], row["advance"]) == (100_000, 0)
    world.db.expire_all()
    from src.models.auth import GroupAudit
    snaps = [a.snapshot for a in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "group_price_set")]
    assert any(s.get("correction_reason") == "опечатка в цене" and s.get("corrected_with_payments") == 1 for s in snaps)


def test_r3_d6_a_correction_is_only_the_correction_of_a_record_of_the_same_day_with_another_sum(world, fixed_today):
    typo_world(world)
    c = owner(world)
    url = f"{API}/org/groups/{world.group.id}/price"
    base = {"correction": True, "correction_reason": "опечатка в цене", "confirm_unusual": True}
    new_date = c.put(url, json={"amount": 500_000, "valid_from": "2026-09-15", **base})            # a NEW day is not a correction: it would move six months of money for three letters
    assert new_date.status_code == 422 and new_date.json()["detail"]["code"] == "CORRECTION_NOT_APPLICABLE"
    same_sum = c.put(url, json={"amount": 5_000_000, "valid_from": "2026-10-01", **base})
    assert same_sum.status_code == 422 and same_sum.json()["detail"]["code"] == "CORRECTION_NOT_APPLICABLE"
    assert c.put(url, json={"amount": 500_000, "valid_from": "2026-10-01", **base}).status_code == 200


def test_r3_d6_a_closed_month_is_not_bypassed_by_a_correction(world, fixed_today, monkeypatch):
    from uuid import uuid4

    from src.auth import closed_period, pricing

    class Closed:
        def closed_for(self, teachers, day):
            return object()

    monkeypatch.setattr(closed_period.ClosedPeriods, "of", staticmethod(lambda db, org: Closed()))
    import pytest

    with pytest.raises(pricing.PriceConflict) as caught:
        pricing.PricingService(world.db)._refuse_closed(str(world.org.id), [{"month": "2026-10", "teacher_id": str(uuid4())}])
    detail = caught.value.detail
    assert detail["code"] == "PERIOD_CLOSED" and "переоткройте месяц" in detail["message"] and detail["can_reopen"] is True


def test_r3_d6_the_individual_price_back_to_the_group_with_a_correction_needs_a_reason_and_is_in_the_audit(world, fixed_today):
    from tests.pg_world import give_price

    m = e6_world(world)
    give_price(world.db, m, 300_000, valid_from=D(2026, 10, 1))                          # the individual price of the same day (a typo to be taken back)
    from datetime import datetime

    from src.models.auth import MembershipPriceOverride

    world.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).update({"created_at": datetime(2026, 10, 2, 8, 0)})
    world.db.commit()
    c = owner(world)
    url = f"{API}/org/memberships/{m.id}/price-override"
    body = {"amount": None, "valid_from": "2026-10-01", "correction": True, "confirm_unusual": True}
    no_reason = c.put(url, json=body)
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "REASON_REQUIRED"      # the old code let «вернуть цену группы» through without a reason
    ok = c.put(url, json={**body, "correction_reason": "ошибочная скидка"})
    assert ok.status_code == 200, ok.text
    snaps = [a.snapshot for a in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "membership_price_set")]
    assert any(x.get("correction_reason") == "ошибочная скидка" and "corrected_with_payments" in x for x in snaps)
    assert c.put(url, json={"amount": 300_000, "valid_from": "2026-09-20", "reason": "скидка", "correction": True, "correction_reason": "x" * 3, "confirm_unusual": True}).json()["detail"]["code"] == "CORRECTION_NOT_APPLICABLE"


def test_r3_k2_the_window_of_a_price_takes_no_row_lock(world, fixed_today):
    from tests.pg_world import capture_queries

    m = e6_world(world)
    c = owner(world)
    for call in (lambda: c.put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": "true"}, json={"amount": 600_000, "confirm_unusual": True}),
                 lambda: c.put(f"{API}/org/memberships/{m.id}/price-override", params={"dry_run": "true"}, json={"amount": 400_000, "reason": "скидка"})):
        statements, r = capture_queries(world, call)
        assert r.status_code == 200, r.text
        assert [s for s in statements if "FOR UPDATE" in s.upper() or "FOR NO KEY UPDATE" in s.upper() or "FOR KEY SHARE" in s.upper()] == []      # the old window held the locks of the group and of the families


def test_r3_k7_a_failure_of_the_measurement_is_logged_with_the_centre_and_the_answer_says_unavailable(world, fixed_today, monkeypatch, caplog):
    from src.auth import money_effects

    e6_world(world)
    monkeypatch.setattr(money_effects, "run_with_effects", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("ERROR"):
        r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": "true"}, json={"amount": 600_000})
    assert r.status_code == 200 and r.json()["money_effects"]["unavailable"] is True
    assert any(str(world.org.id) in rec.getMessage() and "could not be measured" in rec.getMessage() for rec in caplog.records)


def test_v3_an_individual_price_is_checked_the_same_way(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    refused = c.put(f"{API}/org/memberships/{m.id}/price-override", json={"amount": 5_000_000, "reason": "договорённость"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "PRICE_UNUSUAL"
    ok = c.put(f"{API}/org/memberships/{m.id}/price-override", json={"amount": 5_000_000, "reason": "договорённость", "confirm_unusual": True})
    assert ok.status_code == 200, ok.text


# ---- V4: an opening balance on top of payments ---------------------------------------------------------------------------------------------


def test_v4_a_paid_until_on_top_of_payments_names_the_payments_and_the_coverage_it_makes(world, fixed_today):
    from tests.test_opening_bulk_pg import centre, enrolled, it, preview as open_preview

    fixed_today(D(2026, 11, 12))
    centre(world)                                                                     # price 600 000, accounting from 01.11, the shared month
    m = enrolled(world, D(2026, 11, 1))
    seed_payment(world, m, 600_000, D(2026, 11, 2), receipt_no=1)
    item = open_preview(world, [it(world, m, paid_until="2026-12-15")], reason="остаток по журналу")["items"][0]
    over = [x for x in item["warnings"] if x["code"] == "OPENING_OVER_PAYMENTS"]
    assert len(over) == 1 and item["after"]["paid_until"]
    day = D.fromisoformat(item["after"]["paid_until"])
    assert over[0]["message"] == f"У ученика уже есть оплаты на 600{NB}000{NB}сум — покрытие станет до {day.strftime('%d.%m')}"       # the number of the screen «after», the old code was silent
    clean = enrolled(world, D(2026, 11, 1), student=world.students[1])
    assert not [x for x in open_preview(world, [it(world, clean, paid_until="2026-12-15")])["items"][0]["warnings"] if x["code"] == "OPENING_OVER_PAYMENTS"]       # no payments - nothing to say


# ---- V6: the reason of a refund / a correction and the receipt of a refund -----------------------------------------------------------------------


def test_v6_a_refund_and_a_correction_need_a_reason_and_nothing_is_written_without_it(world, fixed_today):
    m = _advance_world(world, fixed_today)
    c = owner(world)
    for body in ({"amount": 100_000, "method": "cash"}, {"amount": 100_000, "method": "cash", "reason": "  "}, {"amount": 100_000, "method": "cash", "reason": "ab"}):
        r = c.post(f"{API}/org/memberships/{m.id}/refunds", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED", body
    assert refunds(world) == []                                                      # the old code returned the money without a word
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).first()
    r = c.post(f"{API}/org/payments/{pay.id}/replace", json={"amount": 700_000, "paid_on": TODAY.isoformat(), "method": "cash"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED"
    world.db.expire_all()
    assert world.db.query(StudentPayment).filter_by(id=pay.id).one().voided_at is None


def test_v6_the_receipt_of_a_refund_has_a_number_the_sum_in_words_the_signature_and_the_note_of_a_cancellation(world, fixed_today):
    from src.auth.money_words import amount_words

    m = _advance_world(world, fixed_today)
    c = owner(world)
    first = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "ученик ушёл"}).json()["refund"]
    second = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 50_000, "method": "card", "reason": "переплата"}).json()["refund"]
    assert (first["receipt_no"], second["receipt_no"]) == (1, 2)
    r = c.get(f"{API}/org/refunds/{first['id']}/receipt")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["receipt_label"] == "В-1" and got["amount"] == 100_000 and got["amount_words"] == amount_words(100_000) and got["reason"] == "ученик ушёл"
    assert got["method_label"] == "Наличные" and got["accepted_by_name"] and got["student_name"] and got["group_name"]
    assert got["disclaimer"] == "Не является фискальным чеком" and got["signature_line"].startswith("Получил(а)") and got["void_note"] is None
    assert c.post(f"{API}/org/refunds/{first['id']}/void", json={"reason": "ошибка"}).status_code == 200
    again = c.get(f"{API}/org/refunds/{first['id']}/receipt").json()
    assert again["void_note"].startswith("Возврат отменён ") and again["void_reason"] == "ошибка"
    assert make_client_for(world, "teacher").get(f"{API}/org/refunds/{first['id']}/receipt").status_code == 403
    assert c.get(f"{API}/org/refunds/{uuid4()}/receipt").status_code == 404


def make_client_for(w, role):
    from tests.pg_world import make_client

    return make_client(w, user={"teacher": w.teacher, "admin": w.admin}[role])


# ---- V5: the receipt -------------------------------------------------------------------------------------------------------------------------


def test_v5_the_first_print_is_the_original_the_next_ones_are_copies_and_a_reading_changes_nothing(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c = owner(world)
    read = c.get(f"{API}/org/payments/{pay.id}/receipt").json()
    assert read["is_copy"] is False and read["print_count"] == 0 and read["disclaimer"] == "Не является фискальным чеком" and read["issued_at"]
    first = c.post(f"{API}/org/payments/{pay.id}/receipt/print")
    assert first.status_code == 200, first.text
    assert first.json()["is_copy"] is False and first.json()["print_count"] == 1
    c.get(f"{API}/org/payments/{pay.id}/receipt")                                       # a reading is not a print
    assert c.get(f"{API}/org/payments/{pay.id}/receipt").json()["print_count"] == 1
    assert c.get(f"{API}/org/payments/{pay.id}/receipt").json()["is_copy"] is True
    world.db.query(StudentPayment).filter_by(id=pay.id).update({"last_printed_at": None})
    world.db.commit()                                                                   # (the debounce of a double click has its own check below)
    second = c.post(f"{API}/org/payments/{pay.id}/receipt/print").json()
    assert second["is_copy"] is True and second["print_count"] == 2


def test_v5_a_double_click_on_print_is_one_print(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c = owner(world)
    a, b = c.post(f"{API}/org/payments/{pay.id}/receipt/print").json(), c.post(f"{API}/org/payments/{pay.id}/receipt/print").json()
    assert (a["print_count"], b["print_count"]) == (1, 1) and a["is_copy"] is False and b["is_copy"] is False


def test_v5_the_state_on_the_day_of_issue_is_the_account_now_not_the_snapshot_of_the_payment(world, fixed_today):
    m = e6_world(world)                                                                 # 400 000 paid for 500 000: debt 100 000 today
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    got = owner(world).get(f"{API}/org/payments/{pay.id}/receipt").json()
    state = got["state_on_issue"]
    assert (state["debt"], state["advance"]) == (100_000, 0) and state["as_of"] == TODAY.isoformat()
    assert state["text"].startswith(f"На {TODAY.strftime('%d.%m.%Y')}: долг 100{NB}000{NB}сум")
    post_payment(world, m, amount=40_000)                                               # later the student pays more: the old receipt shows the debt of TODAY
    assert owner(world).get(f"{API}/org/payments/{pay.id}/receipt").json()["state_on_issue"]["debt"] == 60_000


def test_v5_a_cancelled_payment_prints_with_the_note_and_the_method_of_a_payment_is_mandatory(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c = owner(world)
    assert c.post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошиблись"}).status_code == 200
    got = c.get(f"{API}/org/payments/{pay.id}/receipt").json()
    assert got["void_note"] == f"Оплата отменена {TODAY.strftime('%d.%m')}"
    r = c.post(f"{API}/org/memberships/{m.id}/payments", json={"amount": 10_000, "paid_on": TODAY.isoformat()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "METHOD_REQUIRED"            # the old code wrote «other»


# ---- V10: the small ones and (в) the individual price in the preview of a transfer -----------------------------------------------------------


def second_group(w, price=400_000):
    from tests.pg_world import give_group_price, make_group

    group = make_group(w.db, w.org, w.teacher, "Вторая")
    w.db.commit()
    give_group_price(w.db, group, price, valid_from=D(2026, 1, 1))
    return group


def test_v10c_the_preview_of_a_transfer_says_that_the_individual_price_ends_and_what_comes_instead(world, fixed_today):
    from tests.pg_world import give_price

    e6_world(world)                                                                  # the price of the first group: 500 000
    give_price(world.db, membership(world, world.students[1]), 300_000)             # the student has an individual price
    target = second_group(world)                                                     # 400 000
    c = owner(world)
    got = c.post(f"{API}/org/groups/{target.id}/students/{world.students[1].id}", params={"dry_run": "true"}).json()
    assert got["individual_price_ends"] is True and len(got["closing_prices"]) == 1
    item = got["closing_prices"][0]
    assert (item["amount"], item["group_name"]) == (300_000, world.group.name)
    assert item["text"] == f"У ученика индивидуальная цена 300{NB}000{NB}сум в группе «{world.group.name}»: в новой группе она не действует — будет цена группы 400{NB}000{NB}сум"
    plain = c.post(f"{API}/org/groups/{target.id}/students/{world.students[2].id}", params={"dry_run": "true"}).json()
    assert plain["individual_price_ends"] is False and plain["closing_prices"] == []
    bulk = c.post(f"{API}/org/groups/{target.id}/students/bulk", params={"dry_run": "true"}, json={"student_ids": [str(world.students[1].id), str(world.students[2].id)]}).json()
    assert [x["individual_price_ends"] for x in bulk["students"]] == [True, False]


def test_v10_the_photo_of_a_cheque_goes_to_the_corrected_payment_and_its_removal_leaves_a_line_in_the_audit(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c = owner(world)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    up = c.post(f"{API}/org/payments/{pay.id}/files", files={"file": ("cheque.png", png, "image/png")})
    assert up.status_code == 200, up.text
    new = c.post(f"{API}/org/payments/{pay.id}/replace", json={"amount": 350_000, "paid_on": TODAY.isoformat(), "method": "cash", "reason": "неверная сумма"}).json()["payment"]
    assert [f["filename"] for f in c.get(f"{API}/org/memberships/{m.id}/payments").json()[0]["files"]] == ["cheque.png"]           # the cheque went with the correction (old code: 0 files)
    fid = c.get(f"{API}/org/memberships/{m.id}/payments").json()[0]["files"][0]["id"]
    assert c.delete(f"{API}/org/payments/{new['id']}/files/{fid}").status_code == 200
    snap = audit(world, "payment_file_removed")
    assert snap["payment_id"] == new["id"] and snap["filename"] == "cheque.png" and snap["size_bytes"] == len(png)


def test_v10_the_header_of_the_excel_of_a_month_is_the_totals_of_the_month_and_the_csv_names_the_method_in_russian(world, fixed_today):
    from io import BytesIO

    from openpyxl import load_workbook

    m = e6_world(world)                                                              # October: 500 000 charged on 15.10, 400 000 paid
    c = owner(world)
    xlsx = c.get(f"{API}/org/memberships/{m.id}/statement/export", params={"format": "xlsx", "from": "2026-10-01", "to": "2026-10-31"})
    line = load_workbook(BytesIO(xlsx.content)).active["A3"].value
    assert line.startswith("За период: ") and "Стоимость обучения 500 000" in line and "Оплачено 400 000" in line and "Весь счёт" in line
    csv = c.get(f"{API}/org/memberships/{m.id}/statement/export", params={"format": "csv"}).text
    assert "способ: Наличные" in csv and "способ: cash" not in csv


# ---- V9: the pay of the teacher in the windows of a forgiveness and of an individual price -----------------------------------------------------


def test_v9_the_forgiveness_names_the_period_it_closes_and_that_the_teacher_is_paid_for_it(world, fixed_today):
    m = e6_world(world)                                                              # 500 000 charged on 15.10, 400 000 paid: debt 100 000 of the period 15.10-14.11
    got = owner(world).post(f"{API}/org/memberships/{m.id}/writeoffs", params={"dry_run": "true"}, json={"all": True, "reason": "льгота"}).json()
    block = got["money_effects"]["forgiveness"]
    assert block["period"]["label"] == "15.10–14.11" and block["text"].startswith("Прощение закроет период 15.10–14.11. Эти уроки будут оплачены преподавателю")
    assert got["closes"] == block["period"] and writeoffs(world) == []              # the old window said nothing about the period and the teacher


def test_v9_the_individual_price_says_that_the_pay_of_the_teacher_follows_the_real_price(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    got = c.put(f"{API}/org/memberships/{m.id}/price-override", params={"dry_run": "true"}, json={"amount": 300_000, "reason": "скидка"}).json()
    assert got["teacher_share"]["text"] == "Зарплата преподавателя за этого ученика тоже изменится: процент считается от фактической цены"
    assert "money_effects" in got and got["money_effects"]["dryRun"] is True
    zero = c.put(f"{API}/org/memberships/{m.id}/price-override", params={"dry_run": "true"}, json={"amount": 0, "reason": "бесплатно", "confirm_unusual": True}).json()
    assert zero["teacher_share"]["text"].startswith("Зарплата преподавателя за этого ученика станет 0")
    group = c.put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": "true"}, json={"amount": 600_000}).json()
    assert "money_effects" in group and group["teacher_share"]["text"].startswith("Зарплата преподавателей этой группы тоже изменится")


# ---- R3: one way to create a refund, the winner of a race, photos in the database, the journal --------------------------------------------------


def test_r3_k3_no_second_path_creates_a_refund():
    """Only ``PaymentService._add_refund`` builds a ``StudentRefund`` (R3 К3 = Д5): the window of leaving asks it too."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    builders = {}
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"StudentRefund\(", text):
            line = text[: match.start()].count("\n") + 1
            if text.splitlines()[line - 1].lstrip().startswith("class "):
                continue
            builders.setdefault(str(path.relative_to(root)), []).append(line)
    assert set(builders) <= {"auth/payments_service.py", "auth/leave_service.py"}, builders
    leave = (root / "auth" / "leave_service.py").read_text(encoding="utf-8")
    assert "_add_refund" in leave and leave.count("StudentRefund(") == 1                 # the one construction is the lambda handed to ``_add_refund``


def test_r3_k3_the_refund_of_the_window_of_leaving_goes_through_the_numbered_path(world, fixed_today):
    from tests.pg_world import make_student

    m = e6_world(world)
    seed_payment(world, m, 400_000, D(2026, 10, 16), receipt_no=40)                  # an advance above the price: 800 000 paid, 500 000 charged
    body = {"settle": [{"membership_id": str(m.id), "action": "refund", "method": "cash", "reason": "уход"}]}
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[1].id}/leave", json=body)
    assert r.status_code == 200, r.text
    done = [x for x in r.json()["settled"] if x["action"] == "refund"][0]
    receipt = owner(world).get(f"{API}/org/refunds/{done['refund_id']}/receipt").json()
    assert receipt["receipt_no"] == 1 and receipt["receipt_label"] == "В-1"           # the old path left NULL
    snap = audit(world, "payment_refund")
    assert snap["at_leave"] is True and "advance_after" in snap and "debt_before" in snap
    again = owner(world).post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 1, "method": "cash", "reason": "ещё"})
    assert again.status_code in (201, 422)                                            # the next number is 2, not 1 again (checked in the race test below)


def test_r3_d8_a_race_with_one_key_names_the_record_of_the_winner(world, fixed_today):
    from uuid import UUID

    from src.auth.payments_service import DuplicateRefund, PaymentService
    from src.models.auth import StudentRefund as R
    from tests.test_payment_carry_over_pg import run_in_thread

    m = _advance_world(world, fixed_today)
    key_ = uuid4()
    winner = owner(world).post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 10_000, "method": "cash", "reason": "первый"}, headers={"Idempotency-Key": str(key_)}).json()["refund"]
    svc = PaymentService(world.db)
    with __import__("pytest").raises(DuplicateRefund) as caught:                       # the same key arrives from another family's request that was not serialised by the lock
        svc._add_refund(str(world.org.id), lambda n: R(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, amount=5_000, method="cash",
                                                        reason="гонка", effective_on=TODAY, created_by=world.owner.id, idempotency_key=key_, receipt_no=n))
    assert caught.value.extra.get("refund_id") == winner["id"]


def test_r3_k9_the_photo_of_a_cheque_moves_in_the_database_and_never_through_python(world, fixed_today):
    from tests.pg_world import capture_queries

    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c = owner(world)
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 5000
    assert c.post(f"{API}/org/payments/{pay.id}/files", files={"file": ("a.png", png, "image/png")}).status_code == 200
    statements, r = capture_queries(world, lambda: c.post(f"{API}/org/payments/{pay.id}/replace", json={"amount": 350_000, "paid_on": TODAY.isoformat(), "method": "cash", "reason": "сумма"}))
    assert r.status_code == 201, r.text
    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "student_payment_files" in s and ".data" in s]
    assert reads == []                                                                # no SELECT of the bytes
    assert any("INSERT INTO student_payment_files" in s and "SELECT" in s.upper() for s in statements)
    assert audit(world, "payment_replace")["files_moved"] == 1
    assert world.db.execute(__import__("sqlalchemy").text("SELECT count(*) FROM student_payment_files WHERE size_bytes = :n AND org_id = :o"), {"n": len(png), "o": world.org.id}).scalar() == 2      # the copy and the old one


def test_r3_d11_the_list_of_decisions_says_since_when_it_is_kept(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    first = c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"}).json()
    assert first["since"] is None                                                      # nothing of the new actions yet
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c.post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошибка"})
    assert c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"}).json()["since"] == TODAY.isoformat()


def test_r3_p3_p5_the_collection_row_names_what_the_debtors_owe_and_a_cancelled_here_line_has_its_moment(world, fixed_today):
    fixed_today(D(2026, 11, 12))
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    owner(world).post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошибка"})
    row = owner(world).get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31"}).json()["collection"]["total"]
    assert row["debtors_debt"] == row["debt"] - row["debt_waiting"]
    line = [x for x in journal(world, "2026-11-01", "2026-11-30", cancelled_in_period="true")["rows"] if x["view"] == "cancelled_here"][0]
    assert line["voided_at"] and line["void_reason"] == "ошибка"


# ---- R3 Д4: the warning «оплачено до поверх оплат» in all three places, ONE function ---------------------------------------------------------


def test_r3_d4_the_single_window_of_an_opening_balance_says_it_too(world, fixed_today):
    from datetime import date as _date

    from src.auth import payments_texts as tx

    m = e6_world(world)                                                                 # 400 000 paid
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", params={"dry_run": "true"}, json={"paid_until": "2026-12-15", "reason": "остаток по журналу"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert [w["code"] for w in got["warnings"]] == ["OPENING_OVER_PAYMENTS"]            # the old window: no warnings at all
    assert got["warnings"][0]["message"] == tx.opening_over_payments_text(400_000, _date.fromisoformat(got["row"]["paid_until"]))
    clean = owner(world).put(f"{API}/org/memberships/{membership(world, world.students[2]).id}/opening-balance", params={"dry_run": "true"}, json={"paid_until": "2026-12-15"})
    assert clean.status_code == 200 and clean.json()["warnings"] == []                    # no payments - nothing to say


def test_r3_d4_the_adding_to_a_group_with_an_opening_balance_says_it_through_the_same_function(world, fixed_today):
    from datetime import date as _date

    from src.auth import payments_texts as tx
    from src.auth.leave_service import apply_add_opening
    from src.auth.payments_schemas import OpeningPut

    m = e6_world(world)
    warning = apply_add_opening(world.db, str(world.org.id), m, str(world.owner.id), OpeningPut(paid_until=_date(2026, 12, 15)), transfer=False)
    assert warning and warning["code"] == "OPENING_OVER_PAYMENTS" and warning["message"].startswith(f"У ученика уже есть оплаты на 400{NB}000{NB}сум — покрытие станет до ")
    world.db.rollback()
    assert tx.opening_over_payments_text(1, _date(2026, 1, 20)) == f"У ученика уже есть оплаты на 1{NB}сум — покрытие станет до 20.01"


# ---- R3 К5: two at once (real threads) ---------------------------------------------------------------------------------------------------------

from tests.test_payment_carry_over_pg import engine, run_in_thread  # noqa: E402,F401  (engine = fixture)


def _join(*threads):
    for t in threads:
        t.join(timeout=60)


def test_r3_k5_two_requests_with_one_key_write_one_forgiveness(world, fixed_today, engine):
    import threading

    from src.auth.payments_service import DuplicateWriteoff, PaymentService

    m = e6_world(world)
    barrier, key_ = threading.Barrier(2), uuid4()

    def call(session):
        barrier.wait()
        return PaymentService(session).create_writeoff(str(world.org.id), m.id, str(world.owner.id), amount=20_000, whole=False, reason="льгота", idempotency_key=key_)

    (t1, o1), (t2, o2) = run_in_thread(engine, call), run_in_thread(engine, call)
    _join(t1, t2)
    errors = [o["error"] for o in (o1, o2) if "error" in o]
    assert sum("value" in o for o in (o1, o2)) == 1 and len(errors) == 1 and isinstance(errors[0], DuplicateWriteoff), (o1, o2)
    assert [w.amount for w in writeoffs(world)] == [20_000]


def test_r3_k5_two_different_keys_with_the_sum_they_saw_the_second_gets_balance_changed(world, fixed_today, engine):
    import threading

    from src.auth.payments_service import PaymentService, StaleAmount

    m = e6_world(world)                                                                  # debt 100 000: both saw it and both forgive all of it
    barrier = threading.Barrier(2)

    def call(session):
        barrier.wait()
        return PaymentService(session).create_writeoff(str(world.org.id), m.id, str(world.owner.id), amount=None, whole=True, reason="льгота",
                                                       expected_debt=100_000, idempotency_key=uuid4())

    (t1, o1), (t2, o2) = run_in_thread(engine, call), run_in_thread(engine, call)
    _join(t1, t2)
    errors = [o["error"] for o in (o1, o2) if "error" in o]
    assert sum("value" in o for o in (o1, o2)) == 1 and len(errors) == 1 and isinstance(errors[0], StaleAmount), (o1, o2)
    assert [w.amount for w in writeoffs(world)] == [100_000]


def test_r3_k5_two_refunds_of_two_students_of_one_centre_never_get_one_number(world, fixed_today, engine):
    import threading

    from src.auth.payments_service import PaymentService

    m0 = _advance_world(world, fixed_today)
    m1 = membership(world, world.students[1])
    stay(world, m1, D(2026, 9, 15), D(2026, 10, 25))
    seed_payment(world, m1, 790_000, TODAY, receipt_no=19)
    world.db.commit()
    barrier = threading.Barrier(2)

    def call(mid):
        def run(session):
            barrier.wait()
            return PaymentService(session).create_refund(str(world.org.id), mid, str(world.owner.id), amount=10_000, method="cash", reason="возврат", note=None)
        return run

    (t1, o1), (t2, o2) = run_in_thread(engine, call(m0.id)), run_in_thread(engine, call(m1.id))
    _join(t1, t2)
    assert "value" in o1 and "value" in o2, (o1, o2)
    assert sorted([o1["value"].refund.receipt_no, o2["value"].refund.receipt_no]) == [1, 2]
