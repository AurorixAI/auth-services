"""LOGIC-A: opening balance (decision 1) and pause (decision 2) through the API on PostgreSQL - the golden chains of the spec, the matrix cells that
are about the API (ranges, noop, replace, void, rights, audit, the roster of a lesson)."""
from datetime import date, datetime, timedelta

import pytest

from src.auth import payment_status as ps
from src.models.auth import (
    GroupAudit, MembershipOpeningBalance, MembershipPause, MembershipTransfer, Student, StudentGroup,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    give_group_price, make_client, pg_session, world,
)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, post_payment, pytestmark, seed_payment, settings_block, stay,
)


def centre(w, price=600_000, anchor_day=1):
    w.group.schedule = []            # no schedule: an incomplete period is charged by days
    w.db.commit()
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=anchor_day)
    give_group_price(w.db, w.group, price, valid_from=D(2026, 1, 1))


def enrolled(w, first, student=None):
    m = membership(w, student)
    stay(w, m, first)
    return m


def put(w, m, body, client=None, dry_run=False, expect=200):
    r = (client or owner(w)).put(f"{API}/org/memberships/{m.id}/opening-balance", json=body, params={"dry_run": dry_run})
    assert r.status_code == expect, r.text
    return r.json()


def audit(w, action):
    w.db.expire_all()
    return w.db.query(GroupAudit).filter_by(action=action, org_id=w.org.id).order_by(GroupAudit.created_at).all()


# ---- opening balance --------------------------------------------------------------------------------------------------------------------


def test_opening_a_paid_until_golden(world, fixed_today):
    centre(world)
    m = enrolled(world, D(2026, 10, 21))
    out = put(world, m, {"paid_until": "2026-10-31", "note": "из таблицы Excel"})
    assert out["opening"]["paid_until"] == "2026-10-31" and out["opening"]["source"] == "manual"
    row = out["row"]
    assert (row["debt"], row["advance"], row["status"], row["paid_until"], row["days_left"]) == (0, 0, "active", "2026-10-31", 10)
    assert row["opening"]["paid_until"] == "2026-10-31"
    imp = out["impact"]
    assert (imp["debt_before"], imp["debt_after"], imp["charged_delta"], imp["paid_until_after"]) == (213_000, 0, -213_000, "2026-10-31")
    got = owner(world).get(f"{API}/org/memberships/{m.id}/opening-balance").json()
    assert got["id"] == out["opening"]["id"]
    assert len(audit(world, "opening_balance_set")) == 1


def test_opening_b_debt_and_the_payment_that_closes_it_first(world, fixed_today):
    centre(world)
    m = enrolled(world, D(2026, 10, 21))
    out = put(world, m, {"debt_amount": 250_000, "debt_on": "2026-09-30"})
    row = out["row"]
    assert (row["debt"], row["past_debt"], row["current_due"], row["status"], row["days_overdue"]) == (463_000, 250_000, 213_000, "expired", 21)
    assert row["paid_until"] is None
    pv = owner(world).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 300_000}).json()
    assert pv["result"]["paid_until_after"] == "2026-10-22" and pv["result"]["debt_after"] == 163_000
    assert pv["result"]["summary_text"] == "Закроет долг на начало 250 000 · на текущий период 50 000 → оплачено до 22.10 · останется 163 000"
    created = post_payment(world, m, amount=300_000)
    items = created["allocation"]["items"]
    assert [(a["amount"], a["bucket"], a["closes"]) for a in items] == [(250_000, "past_debt", True), (50_000, "current", False)]
    assert items[0]["label"] == "Закрыла долг на начало учёта на 30.09.2026" and items[1]["segment_remaining_after"] == 163_000
    st = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()
    kinds = [r["type"] for r in st["rows"]]
    assert kinds[0] == "opening_debt" and st["rows"][0]["title"] == "Долг на начало учёта на 30.09.2026" and st["rows"][0]["debit"] == 250_000
    assert st["summary"]["reconciles"] and st["summary"]["debt"] == 163_000


def test_opening_c_both_and_dry_run_writes_nothing(world, fixed_today):
    centre(world)
    m = enrolled(world, D(2026, 10, 21))
    out = put(world, m, {"paid_until": "2026-10-25", "debt_amount": 100_000, "debt_on": "2026-09-30"}, dry_run=True)
    assert out["dry_run"] and out["impact"]["debt_after"] == 100_000 and out["impact"]["paid_until_after"] == "2026-10-25"
    world.db.expire_all()
    assert world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id).count() == 0 and not audit(world, "opening_balance_set")
    put(world, m, {"paid_until": "2026-10-25", "debt_amount": 100_000, "debt_on": "2026-09-30"})
    assert world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id, voided_at=None).count() == 1


def test_opening_ranges_noop_replace_void_and_rights(world, fixed_today):
    centre(world)
    m = enrolled(world, D(2026, 10, 21))
    c = owner(world)
    for body, code in (({"paid_until": "2026-10-20"}, "OPENING_RANGE"), ({"debt_amount": 5, "debt_on": "2026-10-22"}, "OPENING_RANGE"),
                       ({"debt_amount": 5}, "OPENING_RANGE"), ({"debt_on": "2026-09-01"}, "OPENING_RANGE"),
                       ({"debt_amount": 0, "debt_on": "2026-09-01"}, "AMOUNT_RANGE"), ({"paid_until": "2028-10-20"}, "OPENING_RANGE"),
                       ({"debt_amount": 5, "debt_on": "2019-12-31"}, "OPENING_RANGE")):
        r = c.put(f"{API}/org/memberships/{m.id}/opening-balance", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (body, r.text)
    first = put(world, m, {"paid_until": "2026-10-31"})
    again = put(world, m, {"paid_until": "2026-10-31"})                                  # R: the same again = noop
    assert again["opening"]["id"] == first["opening"]["id"] and len(audit(world, "opening_balance_set")) == 1
    replaced = put(world, m, {"paid_until": "2026-11-05", "debt_amount": 1_000, "debt_on": "2026-09-30"})
    assert replaced["opening"]["id"] != first["opening"]["id"]
    assert world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id, voided_at=None).count() == 1
    # B: money exists -> a reason is needed
    post_payment(world, m, amount=100_000)
    r = c.put(f"{API}/org/memberships/{m.id}/opening-balance", json={"paid_until": "2026-11-06"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED"
    ok = put(world, m, {"paid_until": "2026-11-06", "reason": "ошибка в таблице"})
    assert ok["opening"]["paid_until"] == "2026-11-06"
    # U: void by empty body and by id; void twice
    gone = put(world, m, {"reason": "не было остатка"})
    assert gone["opening"] is None and owner(world).get(f"{API}/org/memberships/{m.id}/opening-balance").json() is None
    third = put(world, m, {"paid_until": "2026-11-06", "reason": "вернули"})
    r = c.post(f"{API}/org/opening-balances/{third['opening']['id']}/void", json={"reason": "снова ошибка"})
    assert r.status_code == 200 and r.json()["opening"]["voided_at"] and r.json()["row"]["opening"] is None
    r = c.post(f"{API}/org/opening-balances/{third['opening']['id']}/void", json={"reason": "ещё раз"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_VOIDED"
    assert len(audit(world, "opening_balance_void")) >= 2
    # rights: a teacher never; an admin only with the money switch
    assert make_client(world, user=world.teacher).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"paid_until": "2026-11-06"}).status_code == 403
    adm = make_client(world, user=world.admin)
    assert adm.get(f"{API}/org/memberships/{m.id}/opening-balance").status_code == 403
    settings_block(world, admin_can_manage=True)
    assert adm.get(f"{API}/org/memberships/{m.id}/opening-balance").status_code == 200


def test_opening_not_allowed_after_a_transfer_and_for_an_archived_student(world, fixed_today):
    centre(world)
    m = enrolled(world, D(2026, 10, 21))
    other = enrolled(world, D(2026, 10, 21), world.students[1])
    world.db.add(MembershipTransfer(org_id=world.org.id, student_id=other.student_id, from_membership_id=m.id, to_membership_id=other.id,
                                    at=datetime(2026, 10, 21, 5)))
    world.db.commit()
    r = owner(world).put(f"{API}/org/memberships/{other.id}/opening-balance", json={"paid_until": "2026-10-31"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPENING_NOT_ALLOWED"
    s = world.db.get(Student, m.student_id)
    s.status = "archived"
    world.db.commit()
    r = owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"paid_until": "2026-10-31"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STUDENT_ARCHIVED"


# ---- pause ----------------------------------------------------------------------------------------------------------------------------------


def pause(w, m, first, last, client=None, expect=201, **extra):
    r = (client or owner(w)).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": first, "last_day": last, **extra},
)
    assert r.status_code == expect, r.text
    return r.json()


def chain1_world(w):
    centre(w, 620_000)
    m = enrolled(w, D(2026, 10, 11))
    put(w, m, {"paid_until": "2026-10-15"})
    seed_payment(w, m, 300_000, D(2026, 10, 16))
    return m


def test_pause_golden_chain1_and_void_restores(world, fixed_today):
    m = chain1_world(world)
    row0 = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert (row0["debt"], row0["paid_until"]) == (20_000, "2026-10-30")
    out = pause(world, m, "2026-10-20", "2026-10-24", reason="уехал к родственникам")
    imp, row = out["impact"], out["row"]
    assert (imp["debt_before"], imp["advance_after"], imp["charged_delta"], imp["refundable_after"], imp["paid_until_after"]) == (
        20_000, 80_000, -100_000, 80_000, "2026-11-03")
    assert (row["status"], row["paused"], row["advance"], row["days_left"], row["paid_until"]) == (
        "paused", {"from": "2026-10-20", "to": "2026-10-24"}, 80_000, None, "2026-11-03")
    assert out["pause"]["days"] == 5 and out["payroll"]["months"] == ["2026-10"] and out["lessons"]["attended"] == []
    summ = owner(world).get(f"{API}/org/payments/summary").json()
    assert summ["paused"] == 1
    v = owner(world).post(f"{API}/org/pauses/{out['pause']['id']}/void", json={"reason": "ошибся датой"})
    assert v.status_code == 200, v.text
    back = v.json()
    assert back["row"]["debt"] == 20_000 and back["row"]["status"] != "paused" and back["impact"]["charged_delta"] == 100_000
    assert len(audit(world, "membership_pause_set")) == 1 and len(audit(world, "membership_pause_void")) == 1
    assert owner(world).get(f"{API}/org/memberships/{m.id}/pauses").json()[0]["voided_at"]


def test_pause_dry_run_double_click_overlap_ranges(world, fixed_today):
    m = chain1_world(world)
    c = owner(world)
    r = c.post(f"{API}/org/memberships/{m.id}/pauses", params={"dry_run": True}, json={"first_day": "2026-10-20", "last_day": "2026-10-24"})
    assert r.status_code == 201 and r.json()["dry_run"] and r.json()["row"] is None and r.json()["impact"]["advance_after"] == 80_000
    world.db.expire_all()
    assert world.db.query(MembershipPause).filter_by(org_id=world.org.id).count() == 0
    first = pause(world, m, "2026-10-20", "2026-10-24")
    again = pause(world, m, "2026-10-20", "2026-10-24")                                  # R: the same range = nothing new
    assert again["pause"]["id"] == first["pause"]["id"] and world.db.query(MembershipPause).filter_by(org_id=world.org.id).count() == 1
    r = c.post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-10-24", "last_day": "2026-10-27"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PAUSE_OVERLAP"
    for body, why in (({"first_day": "2026-09-01", "last_day": "2026-09-05"}, "30 days back"), ({"first_day": "2026-10-25", "last_day": "2026-10-24"}, "reversed"),
                      ({"first_day": "2026-10-25", "last_day": "2027-12-31"}, "too long")):
        r = c.post(f"{API}/org/memberships/{m.id}/pauses", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PAUSE_RANGE", why
    # the boundary: exactly 30 days back is allowed
    assert c.post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-09-21", "last_day": "2026-09-22"}).status_code == 201


def test_pause_patch_shorten_extend_and_the_lock_of_old_days(world, fixed_today):
    m = chain1_world(world)
    c = owner(world)
    p = pause(world, m, "2026-10-20", "2026-10-24")["pause"]
    r = c.patch(f"{API}/org/pauses/{p['id']}", json={"last_day": "2026-10-20"})            # «Закончить паузу»
    assert r.status_code == 200 and r.json()["pause"]["days"] == 1 and r.json()["impact"]["charged_delta"] == 80_000
    r = c.patch(f"{API}/org/pauses/{p['id']}", json={"last_day": "2026-10-26"})
    assert r.status_code == 200 and r.json()["pause"]["days"] == 7
    r = c.patch(f"{API}/org/pauses/{p['id']}", json={"last_day": "2026-10-10"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PAUSE_RANGE"
    old = MembershipPause(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id,
                          first_day=D(2026, 8, 1), last_day=D(2026, 8, 10), created_by=world.owner.id)
    world.db.add(old)
    world.db.commit()
    for call in (lambda: c.post(f"{API}/org/pauses/{old.id}/void", json={"reason": "давно"}),
                 lambda: c.patch(f"{API}/org/pauses/{old.id}", json={"last_day": "2026-08-05"})):
        r = call()
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PAUSE_LOCKED", r.text


def test_pause_over_an_attended_lesson_is_refused_and_absent_marks_are_listed(world, fixed_today):
    from tests.pg_world import excused_lesson

    day, lesson_id = excused_lesson(world, complete=True)         # the first student is excused (pending), the others present
    # world students: the first is excused; the second was present
    m2 = membership(world, world.students[1])
    first_day = day - timedelta(days=2)
    fixed_today(day + timedelta(days=3))
    r = owner(world).post(f"{API}/org/memberships/{m2.id}/pauses", json={"first_day": first_day.isoformat(), "last_day": (day + timedelta(days=1)).isoformat()})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PAUSE_OVERLAPS_ATTENDED" and r.json()["detail"]["dates"] == [day.isoformat()]
    m1 = membership(world, world.students[0])
    r = owner(world).post(f"{API}/org/memberships/{m1.id}/pauses", json={"first_day": first_day.isoformat(), "last_day": (day + timedelta(days=1)).isoformat()})
    assert r.status_code == 201, r.text
    assert [(x["date"], x["marks"]) for x in r.json()["lessons"]["excluded"]] == [(day.isoformat(), "excused")]


def test_pause_rights_closed_membership_and_the_roster_of_a_lesson(world, fixed_today):
    m = chain1_world(world)
    assert make_client(world, user=world.teacher).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-10-20", "last_day": "2026-10-21"}).status_code == 403
    adm = make_client(world, user=world.admin)                                              # an admin without the money switch: no sums, no row
    out = adm.post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-10-21", "last_day": "2026-10-24"})
    assert out.status_code == 201 and out.json()["impact"] is None and out.json()["row"] is None and out.json()["payroll"] is None
    # the student on a pause is not in the roster of a lesson started today
    roster = world.svc._active_attendance_roster(world.group.id)
    assert str(m.student_id) not in {r["student_id"] for r in roster} and len(roster) == 2
    v = adm.post(f"{API}/org/pauses/{out.json()['pause']['id']}/void", json={"reason": "вернулся"})
    assert v.status_code == 200 and v.json()["impact"] is None
    assert str(m.student_id) in {r["student_id"] for r in world.svc._active_attendance_roster(world.group.id)}
    m.is_active = False
    world.db.commit()
    r = owner(world).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-10-21", "last_day": "2026-10-22"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MEMBERSHIP_CLOSED"
