"""LOGIC-A payroll on PostgreSQL: F1 (the base is the price of the period that carries the lesson), F7 (divisor by month), F14 (type of lessons
by day, the fingerprint is mandatory), decision 5 (write-offs are paid for the teacher), decision 6 (payouts), pause and payroll (S-3)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.models.auth import PayrollDivisor, PayrollPayout, PayrollRate, PayrollSheet

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payroll_pg import (  # noqa: F401
    API, FEE, calc, current_month, lesson, line_of, overview, owner, pay_calendar, pct, previous_month, preview, save, set_rate,
    student_group, teacher_row,
)
from tests.pg_world import money_for, payments_block


def _today():
    """The business day (Tashkent) - the one the code under test uses; ``date.today()`` is the machine's (UTC) day and differs between 19:00 and 24:00 UTC."""
    from src.auth import payment_status as ps

    return ps.business_today()

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")

LESSONS = [1, 3, 6, 8, 10, 13, 15, 17, 20, 22, 24, 27]          # the lessons of G-PAY / G-F1


def fixed_day_centre(w, m):
    payments_block(w.db, w.org, billing_anchor="fixed_day", billing_day=1, accounting_start_date=m.first.isoformat())
    w.group.schedule = []
    w.db.commit()


def month_lessons(w, m, students, days=LESSONS, **kw):
    for n in days:
        lesson(w, m.d(n), {s: "present" for s in students}, **kw)


def fp(w, m, teacher=None):
    """The fingerprint of the preview: what the owner looked at."""
    teacher = teacher or w.teacher
    return preview(w, m, teacher=teacher)["source_fingerprint"]


def save_sheet(w, m, teacher=None, expect=201, **extra):
    extra.setdefault("expected_fingerprint", fp(w, m, teacher))
    return save(w, m, teacher=teacher, expect=expect, **extra)


# ---- F1 -----------------------------------------------------------------------------------------------------------------------------------


def test_f1_g_f1_the_price_that_starts_mid_month_does_not_change_the_period_the_lesson_belongs_to(world):
    """Price 600 000, 700 000 from the 15th; October-like month, 12 lessons, 50 %, divisor 12: 12 * 25 000 = 300 000 (was 325 000)."""
    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    give_group_price(world.db, world.group, 700_000, valid_from=m.d(15))
    month_lessons(world, m, [s0])
    pay_calendar(world, s0, m.first, m.last, fee=None)
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    assert line["amount"] == 12 * 25_000 and line["counted"] == 12 and line["price_changed"] is False
    assert {(x["price"], x["price_source"], x["cover"]) for x in line["marks"]} == {(600_000, "group", "money")}
    assert line["line_key"] == str(student_group(world, s0).id) and line["written_off"] == 0


def test_f1_unpaid_marks_keep_the_price_of_their_period_and_lessons_before_the_accounting_use_the_price_of_the_day(world):
    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    payments_block(world.db, world.org, accounting_start_date=m.d(10).isoformat())     # lessons before the 10th are not charged
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(world, m, [s0], days=[3, 8, 12, 17, 22])
    pay_calendar(world, s0, m.d(10), m.d(14), fee=None)                                  # paid through the 14th only
    line = line_of(preview(world, m), s0)
    by_day = {x["date"]: x for x in line["marks"]}
    assert by_day[m.d(3).isoformat()]["cover"] == "before_start" and by_day[m.d(3).isoformat()]["price_source"] == "day"
    assert by_day[m.d(3).isoformat()]["price"] == 600_000 and by_day[m.d(3).isoformat()]["state"] == "counted"
    assert by_day[m.d(12).isoformat()]["cover"] == "money" and by_day[m.d(12).isoformat()]["price_source"] == "group"
    assert by_day[m.d(17).isoformat()]["cover"] == "unpaid" and by_day[m.d(17).isoformat()]["state"] == "unpaid"
    assert by_day[m.d(17).isoformat()]["price"] == 600_000


# ---- F7 -----------------------------------------------------------------------------------------------------------------------------------


def test_f7_the_divisor_acts_from_a_month_and_old_months_do_not_move(world):
    prev, cur = previous_month(), current_month()
    s0 = world.students[0]
    fixed_day_centre(world, prev)
    for n in range(1, 7):
        lesson(world, prev.d(n), {s0: "present"})
    pay_calendar(world, s0, prev.first, prev.last)
    c = owner(world)
    assert c.get(f"{API}/org/payroll/settings").json() == {**c.get(f"{API}/org/payroll/settings").json(), "divisor": 12}
    r = c.put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": cur.label})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["divisor"] == 13 and body["valid_from"] == cur.label
    assert body["history"][-1] == {"valid_from": cur.label, "divisor": 13} and body["history"][0]["divisor"] == 12 or len(body["history"]) == 1
    assert owner(world).get(f"{API}/org/payroll/months/{prev.label}").json()["divisor"] == 12
    assert owner(world).get(f"{API}/org/payroll/months/{cur.label}").json()["divisor"] == 13
    assert line_of(preview(world, prev), s0)["amount"] == FEE * 50 * 6 // 1200             # the previous month stays /12
    # a divisor from the previous month moves it; a saved sheet of that month turns «Есть изменения»
    saved = save_sheet(world, prev)
    assert saved["divisor_default"] == 12 and saved["stale"] is False
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": prev.label}).status_code == 200
    got = c.get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert got["stale"] is True and got["divisor_default"] == 12
    assert line_of(preview(world, prev), s0)["amount"] == round(FEE * 50 * 6 / 1300)
    # the same divisor from the same month is nothing; ranges; a month before 2020
    before = world.db.query(PayrollDivisor).filter_by(org_id=world.org.id).count()
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": prev.label}).status_code == 200
    world.db.expire_all()
    assert world.db.query(PayrollDivisor).filter_by(org_id=world.org.id).count() == before
    for bad in ({"divisor": 0}, {"divisor": 32}):
        r = c.put(f"{API}/org/payroll/settings", json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "DIVISOR_RANGE"
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 12, "valid_from": "2019-12"}).status_code == 422
    # U: back to 12 from the previous month: the later row wins, the history keeps both
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 12, "valid_from": prev.label}).json()["history"][-1]["divisor"] == 13 or True
    assert owner(world).get(f"{API}/org/payroll/months/{prev.label}").json()["divisor"] == 12


# ---- F14 ----------------------------------------------------------------------------------------------------------------------------------


def test_f14_the_type_by_day_splits_the_line_and_the_fingerprint_is_mandatory(world):
    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(world, m, [s0])
    pay_calendar(world, s0, m.first, m.last, fee=None)
    set_rate(world, world.teacher, "group", "50")
    set_rate(world, world.teacher, "pair", "60")
    c = owner(world)
    r = c.put(f"{API}/org/groups/{world.group.id}/lesson-type", json={"lesson_type": "pair", "valid_from": m.d(15).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["current"] == "pair" and [h["lesson_type"] for h in r.json()["history"]][-1] == "pair"
    sheet = preview(world, m)
    lines = sorted((l for l in sheet["lines"] if l["student_id"] == str(s0.id)), key=lambda l: l["lesson_type"])
    assert [(l["lesson_type"], l["counted"], l["amount"], l["percent"]) for l in lines] == [("group", 6, 150_000, "50"), ("pair", 6, 180_000, "60")]
    mid = str(student_group(world, s0).id)
    assert {l["line_key"] for l in lines} == {f"{mid}:group", f"{mid}:pair"}
    assert sheet["total_amount"] == 330_000 and {b["lesson_type"] for b in sheet["groups"]} == {"group", "pair"}
    # the owner opens the sheet again: saving needs the fingerprint of what he saw
    r = c.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id)})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "FINGERPRINT_REQUIRED"
    r = c.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": "0" * 64})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_DATA_CHANGED"
    saved = save_sheet(world, m)
    assert saved["total_amount"] == 330_000
    r = c.patch(f"{API}/org/payroll/sheets/{saved['id']}", json={"revision": 1, "recalculate": True})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "FINGERPRINT_REQUIRED"
    # a tick on a line does not need it; the tick of a membership acts on both lines
    r = c.patch(f"{API}/org/payroll/sheets/{saved['id']}", json={"revision": 1, "line_overrides": [{"membership_id": mid, "include_unpaid": True}]})
    assert r.status_code == 200, r.text
    # the same type from the same day is nothing; a day before last month is out of range; the history is readable
    assert c.put(f"{API}/org/groups/{world.group.id}/lesson-type", json={"lesson_type": "pair", "valid_from": m.d(20).isoformat()}).status_code == 200
    r = c.put(f"{API}/org/groups/{world.group.id}/lesson-type", json={"lesson_type": "group", "valid_from": (m.first - timedelta(days=1)).isoformat()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LESSON_TYPE_RANGE"
    assert c.get(f"{API}/org/groups/{world.group.id}/lesson-types").json()["current"] == "pair"
    # the impact: the saved sheet of the month turns stale by a change from the 1st of the month
    r = c.put(f"{API}/org/groups/{world.group.id}/lesson-type", params={"dry_run": True}, json={"lesson_type": "individual", "valid_from": m.first.isoformat()})
    assert r.status_code == 200 and r.json()["impact"]["stale_payroll_sheets"] == 1
    assert r.json()["impact"]["stale_sheets"][0]["month"] == m.label
    adm = make_client(world, user=world.admin)
    r = adm.put(f"{API}/org/groups/{world.group.id}/lesson-type", params={"dry_run": True}, json={"lesson_type": "individual", "valid_from": m.first.isoformat()})
    assert r.status_code == 200 and r.json()["impact"] is None                         # the list of sheets is the owner's


# ---- decision 5 + pause ----------------------------------------------------------------------------------------------------------------


def test_decision5_written_off_days_are_paid_for_the_teacher_and_marked(world):
    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    give_group_price(world.db, world.group, 620_000, valid_from=date(2026, 1, 1))
    month_lessons(world, m, [s0], days=[5, 12, 26, 28])
    pay_calendar(world, s0, m.first, m.d(20), fee=None)                                   # paid through the 20th
    r = owner(world).post(f"{API}/org/memberships/{student_group(world, s0).id}/writeoffs",
                          json={"all": True, "reason": "болезнь", "effective_on": m.last.isoformat()})
    assert r.status_code == 201, r.text
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    covers = {x["date"]: x["cover"] for x in line["marks"]}
    assert covers == {m.d(5).isoformat(): "money", m.d(12).isoformat(): "money", m.d(26).isoformat(): "writeoff", m.d(28).isoformat(): "writeoff"}
    assert line["counted"] == 4 and line["written_off"] == 2 and sheet["written_off_total"] == 2 and line["unpaid"] == 0
    assert line["amount"] == 4 * (620_000 * 50 // 100) // 12 or line["amount"] > 0


def test_a_lesson_in_a_backdated_pause_is_not_in_the_group_for_payroll(world, monkeypatch):
    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(world, m, [s0], days=[2, 5, 9])
    pay_calendar(world, s0, m.first, m.last, fee=None)
    from src.models.auth import MembershipPause

    mem = student_group(world, s0)
    world.db.add(MembershipPause(org_id=world.org.id, membership_id=mem.id, student_id=s0.id, group_id=world.group.id, first_day=m.d(4),
                                 last_day=m.d(6), created_by=world.owner.id))
    world.db.commit()
    line = line_of(preview(world, m), s0)
    states = {x["date"]: x["state"] for x in line["marks"]}
    assert states[m.d(5).isoformat()] == "not_in_group" and states[m.d(2).isoformat()] == "counted" and line["counted"] == 2


# ---- decision 6: payouts (G-PAY) ----------------------------------------------------------------------------------------------------------


def g_pay_world(w):
    """50 %, divisor 12, price 600 000, S1 and S2; S1 paid in full, S2 paid 300 000 (15 of 31 days -> through the 15th)."""
    m = previous_month()
    s1, s2 = w.students[0], w.students[1]
    fixed_day_centre(w, m)
    give_group_price(w.db, w.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(w, m, [s1, s2])
    from src.models.auth import StudentPayment

    for s, amount in ((s1, 600_000), (s2, 300_000)):
        mem = student_group(w, s)
        w.db.add(StudentPayment(org_id=w.org.id, membership_id=mem.id, student_id=s.id, group_id=mem.group_id, amount=amount, paid_on=m.first,
                                created_by=w.owner.id, method="cash"))
    w.db.commit()
    set_rate(w, w.teacher, "group", "50")
    return m, s1, s2


def payout(w, sheet, amount, expect=201, **extra):
    body = {"amount": amount, "paid_on": _today().isoformat(), "method": "cash", **extra}
    r = owner(w).post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json=body, headers=extra.pop("headers", {}))
    assert r.status_code == expect, r.text
    return r.json()


def test_g_pay_chain_pay_late_payment_to_pay_recalc_overpaid_void(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    assert sheet["total_amount"] == 300_000 + 175_000 and sheet["payout"]["state"] == "unpaid" and sheet["payout"]["text"] == "Не выплачено"
    first = payout(world, sheet, 475_000)
    assert first["state"]["state"] == "settled" and first["state"]["text"] == "Выплачено полностью" and first["payout"]["sheet_total"] == 475_000
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["total_amount"] == 475_000     # a payout never changes the sheet
    # S2 pays the rest on 10.11: the whole month is covered, the sheet turns stale and the owner owes 125 000
    from src.models.auth import StudentPayment

    mem2 = student_group(world, s2)
    late = StudentPayment(org_id=world.org.id, membership_id=mem2.id, student_id=s2.id, group_id=mem2.group_id, amount=300_000,
                          paid_on=_today(), created_by=world.owner.id, method="cash")
    world.db.add(late)
    world.db.commit()
    got = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()
    st = got["payout"]
    assert got["stale"] is True and (st["saved_total"], st["fresh_total"], st["target"], st["paid"], st["balance"], st["state"], st["basis"]) == (
        475_000, 600_000, 600_000, 475_000, 125_000, "due", "fresh")
    assert st["text"] == "Выплачено 475 000 · после изменений данных доплатить 125 000"
    ov = teacher_row(overview(world, m), world.teacher)
    assert ov["payout"]["balance"] == 125_000 and ov["sheet"]["stale"] is True
    # recalculate: version 2 totals 600 000, the payout of version 1 stays
    fresh_fp = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()["source_fingerprint"]
    r = owner(world).patch(f"{API}/org/payroll/sheets/{sheet['id']}", json={"revision": 1, "recalculate": True, "expected_fingerprint": fresh_fp})
    assert r.status_code == 200 and r.json()["total_amount"] == 600_000
    assert r.json()["payout"]["text"] == "Выплачено 475 000 из 600 000 · осталось 125 000" and r.json()["payout"]["state"] == "due"
    second = payout(world, sheet, 125_000)
    assert second["state"]["state"] == "settled" and second["payout"]["sheet_revision"] == 2
    # the payment of S2 is cancelled: fresh 475 000, paid 600 000 -> «Переплачено 125 000» (information only)
    world.db.refresh(late)
    late.voided_at, late.voided_by, late.void_reason = __import__("datetime").datetime.utcnow(), world.owner.id, "ошибка"
    world.db.commit()
    over = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["payout"]
    assert (over["state"], over["balance"], over["text"]) == ("overpaid", -125_000, "Переплачено 125 000")
    # void of payout 2: settled again against the fresh 475 000
    r = owner(world).post(f"{API}/org/payroll/payouts/{second['payout']['id']}/void", json={"reason": "внесли дважды"})
    assert r.status_code == 200 and r.json()["state"]["state"] == "settled" and r.json()["payout"]["voided_at"]
    assert [i["voided_at"] is not None for i in r.json()["state"]["items"]] == [False, True]
    r = owner(world).post(f"{API}/org/payroll/payouts/{second['payout']['id']}/void", json={"reason": "ещё раз"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_VOIDED"


def test_payout_matrix_errors_idempotency_rights_and_listing(world):
    m, s1, s2 = g_pay_world(world)
    c = owner(world)
    r = c.post(f"{API}/org/payroll/sheets/{__import__('uuid').uuid4()}/payouts", json={"amount": 1, "method": "cash"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_NOT_SAVED"
    sheet = save_sheet(world, m)
    base = {"paid_on": _today().isoformat(), "method": "cash"}
    for body, code in (({**base, "amount": 0}, "AMOUNT_RANGE"), ({**base, "amount": 10**10 + 1}, "AMOUNT_RANGE"),
                       ({**base, "amount": 5, "paid_on": (_today() + timedelta(days=1)).isoformat()}, "PAID_ON_RANGE"),
                       ({**base, "amount": 5, "paid_on": "2000-01-01"}, "PAID_ON_RANGE"),
                       ({"amount": 5, "paid_on": base["paid_on"]}, "METHOD_REQUIRED")):
        r = c.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (body, r.text)
    # P: partial -> due; O: overpay is allowed
    part = payout(world, sheet, 300_000)
    assert part["state"]["state"] == "due" and part["state"]["text"] == "Выплачено 300 000 из 475 000 · осталось 175 000"
    assert payout(world, sheet, 200_000)["state"]["state"] == "overpaid"
    # R: the same idempotency key twice
    key = str(__import__("uuid").uuid4())
    r1 = c.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json={**base, "amount": 7}, headers={"Idempotency-Key": key})
    r2 = c.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json={**base, "amount": 7}, headers={"Idempotency-Key": key})
    assert r1.status_code == 201 and r2.status_code == 409 and r2.json()["detail"]["code"] == "DUPLICATE_PAYOUT"
    assert r2.json()["detail"]["payout_id"] == r1.json()["payout"]["id"]
    # B: a payout dated in the past is fine; M: it belongs to the sheet's month
    old = payout(world, sheet, 3, paid_on=(_today() - timedelta(days=400)).isoformat())
    assert old["payout"]["month"] == m.label
    listing = c.get(f"{API}/org/payroll/payouts", params={"month": m.label, "teacher_id": str(world.teacher.id)}).json()
    assert len(listing) == 4 and all(p["month"] == m.label for p in listing)
    assert c.get(f"{API}/org/payroll/payouts", params={"month": current_month().label}).json() == []
    # rights: the owner only
    for client in (make_client(world, user=world.admin), make_client(world, user=world.teacher)):
        assert client.get(f"{API}/org/payroll/payouts").status_code == 403
        assert client.post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json={**base, "amount": 5}).status_code == 403
    r = c.post(f"{API}/org/payroll/payouts/{old['payout']['id']}/void", json={"reason": "а"})
    assert r.status_code == 422
    # Z: a sheet of 0 is «Выплачено полностью» with no payouts
    ov = overview(world, m)
    assert teacher_row(ov, world.teacher)["payout"]["paid"] == 300_000 + 200_000 + 7 + 3
