"""OPENING-BULK S0: the range of an opening balance is ``L = max(first_day, accounting_start - 1)`` - «долг на 31.10» and «оплачено до 31.10» of a
student who came in September work when the accounting starts on 01.11.  «Оплачено до» = the day before the start is «nothing owed at the start»
(no row).  One ``validate`` serves the single PUT, the import, «Добавить в группу» and the batch."""
from datetime import date, datetime, timedelta

from src.auth import payment_status as ps
from src.models.auth import GroupAudit, MembershipOpeningBalance

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, pytestmark, settings_block, stay,
)


def centre_nov(w, price=600_000):
    w.group.schedule = []
    w.db.commit()
    settings_block(w, accounting_start_date="2026-11-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, price, valid_from=D(2026, 1, 1))


def put(w, m, body, expect=200, client=None):
    r = (client or owner(w)).put(f"{API}/org/memberships/{m.id}/opening-balance", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def live(w):
    w.db.expire_all()
    return w.db.query(MembershipOpeningBalance).filter_by(org_id=w.org.id, voided_at=None).all()


def test_s0_debt_on_the_day_before_the_start_for_a_student_of_september(world, fixed_today):
    centre_nov(world)
    m = membership(world)
    stay(world, m, D(2026, 9, 1))
    out = put(world, m, {"debt_amount": 550_000, "debt_on": "2026-10-31"})
    assert out["opening"]["debt_on"] == "2026-10-31" and out["row"]["debt"] == 550_000
    st = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()
    first = st["rows"][0]
    assert first["type"] == "opening_debt" and first["debit"] == 550_000 and "31.10.2026" in first["title"]
    assert st["summary"]["reconciles"] and st["summary"]["debt"] == 550_000
    # a day later than L is still refused, and the hint names the day
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"debt_amount": 5, "debt_on": "2026-11-01"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "OPENING_RANGE" and "31.10.2026" in r.json()["detail"]["message"]


def test_s0_paid_until_the_day_before_the_start_is_no_opening(world, fixed_today):
    centre_nov(world)
    m = membership(world)
    stay(world, m, D(2026, 9, 1))
    out = put(world, m, {"paid_until": "2026-10-31"})
    assert out["opening"] is None and not live(world)                       # no row, not an error
    assert world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="opening_balance_set").count() == 0
    # with a live opening it voids it (the same as the empty body)
    put(world, m, {"debt_amount": 100_000, "debt_on": "2026-10-31"})
    assert len(live(world)) == 1
    gone = put(world, m, {"paid_until": "2026-10-31"})
    assert gone["opening"] is None and not live(world)
    assert world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="opening_balance_void").count() == 1
    # together with a debt: only the debt is kept
    both = put(world, m, {"paid_until": "2026-10-31", "debt_amount": 70_000, "debt_on": "2026-10-31"})
    assert both["opening"]["paid_until"] is None and both["opening"]["debt_amount"] == 70_000


def test_s0_paid_until_in_november_pays_november(world, fixed_today):
    centre_nov(world)
    m = membership(world)
    stay(world, m, D(2026, 9, 1))
    out = put(world, m, {"paid_until": "2026-11-30"})
    assert out["opening"]["paid_until"] == "2026-11-30"
    assert (out["row"]["debt"], out["row"]["paid_until"]) == (0, "2026-11-30")
    fixed_today(D(2026, 11, 20))
    row = put(world, m, {"paid_until": "2026-11-30"})["row"]
    assert (row["debt"], row["advance"], row["status"], row["paid_until"]) == (0, 0, "active", "2026-11-30")
    fixed_today(D(2026, 12, 3))
    got = owner(world).get(f"{API}/org/memberships/{m.id}/opening-balance").json()
    assert got["paid_until"] == "2026-11-30"


def test_s0_paid_until_before_l_is_refused_with_the_hint(world, fixed_today):
    centre_nov(world)
    m = membership(world)
    stay(world, m, D(2026, 9, 1))
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"paid_until": "2026-10-15"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "OPENING_RANGE"
    assert "внесите как долг" in r.json()["detail"]["message"] and "31.10.2026" in r.json()["detail"]["message"]
    assert not live(world)


def test_s0_old_cases_a_student_who_came_after_the_start_and_first_day_bounds(world, fixed_today):
    centre_nov(world)
    m = membership(world)
    stay(world, m, D(2026, 11, 5))                                            # came after the start: L = first_day
    c = owner(world)
    for body in ({"paid_until": "2026-11-04"}, {"paid_until": "2026-10-31"}, {"debt_amount": 5, "debt_on": "2026-11-06"}):
        r = c.put(f"{API}/org/memberships/{m.id}/opening-balance", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "OPENING_RANGE", body
    assert "внесите как долг" not in c.put(f"{API}/org/memberships/{m.id}/opening-balance", json={"paid_until": "2026-11-04"}).json()["detail"]["message"]
    assert put(world, m, {"paid_until": "2026-11-05"})["opening"]["paid_until"] == "2026-11-05"   # L itself
    assert put(world, m, {"debt_amount": 5, "debt_on": "2026-11-05"})["opening"]["debt_on"] == "2026-11-05"
    # a student who came exactly on the start: L = the start
    m2 = membership(world, world.students[1])
    stay(world, m2, D(2026, 11, 1))
    assert owner(world).put(f"{API}/org/memberships/{m2.id}/opening-balance", json={"paid_until": "2026-10-31"}).status_code == 422
    assert put(world, m2, {"paid_until": "2026-11-01"})["opening"]["paid_until"] == "2026-11-01"


def test_s0_import_and_add_to_group_use_the_same_floor(world, fixed_today):
    from src.auth import import_service as imp

    errs, warns = [], []
    err = lambda *a: errs.append(a)
    warn = lambda *a: warns.append(a)
    start, enrol = D(2026, 11, 1), D(2026, 10, 21)                       # enrolled today, the accounting starts on 01.11
    d = {imp.K_PAID_UNTIL: "31.10.2026", imp.K_DEBT: "", imp.K_DEBT_ON: ""}
    ok, paid, debt, on = imp.parse_opening(d, 2, money=True, accounting_start=start, enrol_day=enrol, err=err, warn=warn)
    assert (ok, paid, debt, on) == (True, None, None, None) and not warns        # nothing owed at the start: no opening, no warning
    d = {imp.K_PAID_UNTIL: "15.10.2026", imp.K_DEBT: "", imp.K_DEBT_ON: ""}
    ok, paid, *_ = imp.parse_opening(d, 3, money=True, accounting_start=start, enrol_day=enrol, err=err, warn=warn)
    assert paid is None and len(warns) == 1
    d = {imp.K_PAID_UNTIL: "30.11.2026", imp.K_DEBT: "250 000", imp.K_DEBT_ON: ""}
    ok, paid, debt, on = imp.parse_opening(d, 4, money=True, accounting_start=start, enrol_day=enrol, err=err, warn=warn)
    assert (paid, debt, on) == (D(2026, 11, 30), 250_000, D(2026, 10, 31))        # the default day of a debt: the day before the start
    d = {imp.K_DEBT: "250 000", imp.K_DEBT_ON: "01.11.2026"}
    ok, paid, debt, on = imp.parse_opening(d, 5, money=True, accounting_start=start, enrol_day=enrol, err=err, warn=warn)
    assert debt is None and len(warns) == 2 and not errs
