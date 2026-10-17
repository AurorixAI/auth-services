"""ROADMAP #105: the price of a group and the individual price of a student - API, payments, payroll per lesson date,
stale sheets, permissions, locks, the dry-run script (PostgreSQL)."""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from src.auth import pricing
from src.auth.service import AuthService
from src.models.auth import (
    GroupAttendance, GroupAudit, GroupPrice, MembershipPriceOverride, Organization, Student, StudentGroup,
    StudentPayment,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    EPOCH, DB_URL, business_today, give_group_price, give_price, make_client, make_group, make_member, make_student,
    make_user, needs_pg, pg_session, world,
)
from tests.test_payment_carry_over_pg import engine, run_in_thread, wait_until_blocked  # noqa: F401  (engine = fixture)
from tests.test_payments_p22_pg import D, quote, setup_mwf  # noqa: F401
from tests.test_payments_service_pg import API, day, mid, owner as money_owner, pay, row_of, set_settings
from tests.test_payroll_pg import (
    count_queries, current_month, lesson, line_of, overview, pay_calendar, preview, previous_month,
    save as save_sheet, teacher_row, workbook,
)

pytestmark = needs_pg


# ---- helpers ------------------------------------------------------------------------------------------------------


def put_price(w, amount=500_000, group=None, valid_from=None, note=None, client=None, expect=200, dry=False, **extra):
    body = {"amount": amount, **extra}
    if valid_from is not None:
        body["valid_from"] = valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from
    if note is not None:
        body["note"] = note
    r = (client or money_owner(w)).put(
        f"{API}/org/groups/{(group or w.group).id}/price" + ("?dry_run=1" if dry else ""), json=body)
    assert r.status_code == expect, r.text
    return r.json()


def get_prices(w, group=None, client=None, expect=200):
    r = (client or money_owner(w)).get(f"{API}/org/groups/{(group or w.group).id}/prices")
    assert r.status_code == expect, r.text
    return r.json()


def put_override(w, student=None, group=None, amount=300_000, reason="скидка", valid_from=None, client=None, expect=200):
    body = {"amount": amount}
    if reason is not None:
        body["reason"] = reason
    if valid_from is not None:
        body["valid_from"] = valid_from.isoformat()
    r = (client or money_owner(w)).put(
        f"{API}/org/memberships/{mid(w, student, group)}/price-override", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def get_override(w, student=None, group=None, client=None, expect=200):
    r = (client or money_owner(w)).get(f"{API}/org/memberships/{mid(w, student, group)}/price-override")
    assert r.status_code == expect, r.text
    return r.json()


def group_rows(w, group=None):
    w.db.expire_all()
    return [(r.valid_from, r.amount, r.note) for r in
            w.db.query(GroupPrice).filter_by(group_id=(group or w.group).id).order_by(GroupPrice.valid_from)]


def allow_admin(w):
    w.db.query(Organization).filter_by(id=w.org.id).update({"settings": {"payments": {"admin_can_manage": True}}})
    w.db.commit()


def month_lessons(w, m, days, students=None):
    for n in days:
        lesson(w, m.d(n), {s: "present" for s in (students or w.students)})


def paid_month(w, m):
    for s in w.students:
        pay_calendar(w, s, m.first, m.last, fee=None)          # the price comes from the group, not from the payment


# ============================================================================================
# the group price: reading, writing, history
# ============================================================================================


def test_a_group_without_a_price_and_the_first_price(world):
    empty = get_prices(world)
    assert empty["current"] is None and empty["history"] == [] and empty["group_name"] == "Группа"
    assert empty["period_mode"] == "calendar" and empty["price_label"] == "Оплата в месяц"
    assert empty["as_of"] == business_today().isoformat()
    out = put_price(world, 450_000, note="Сентябрь")
    assert out["dry_run"] is False
    assert out["current"] == {"amount": 450_000, "valid_from": business_today().isoformat(), "note": "Сентябрь"}   # the default is today
    (row,) = out["history"]
    assert row["amount"] == 450_000 and row["is_current"] is True and row["is_future"] is False
    assert row["created_by"] == str(world.owner.id) and row["created_by_name"] == "Иван Owner" and row["created_at"]
    assert get_prices(world) == {k: v for k, v in out.items() if k not in ("dry_run", "impact")}
    set_settings(world, period_mode="lessons")
    assert get_prices(world)["price_label"] == "Стоимость пакета"                   # the meaning follows the billing mode


def test_the_history_keeps_every_step_and_the_price_on_a_day_is_the_last_one_not_after_it(world):
    put_price(world, 400_000, valid_from=day(-60))
    put_price(world, 500_000, valid_from=day(-10))
    put_price(world, 600_000, valid_from=day(30), note="с нового сезона")
    got = get_prices(world)
    assert [(r["amount"], r["valid_from"], r["is_current"], r["is_future"]) for r in got["history"]] == [
        (600_000, day(30).isoformat(), False, True),
        (500_000, day(-10).isoformat(), True, False),
        (400_000, day(-60).isoformat(), False, False),
    ]
    assert got["current"]["amount"] == 500_000 and got["current"]["valid_from"] == day(-10).isoformat()
    # the same day again corrects that day (one row per group and day), the others stay
    put_price(world, 550_000, valid_from=day(-10), note="исправлено")
    assert group_rows(world) == [(day(-60), 400_000, None), (day(-10), 550_000, "исправлено"), (day(30), 600_000, "с нового сезона")]


def test_a_price_is_validated(world):
    for body in ({"amount": -1}, {"amount": 10_000_000_001}, {"amount": "abc"}, {"amount": 1.5}, {},
                 {"amount": 1, "note": "я" * 201}, {"amount": 1, "valid_from": "вчера"}):
        r = money_owner(world).put(f"{API}/org/groups/{world.group.id}/price", json=body)
        assert r.status_code == 422, body
    put_price(world, 0)                                                              # free is a price
    put_price(world, 10_000_000_000, valid_from=day(-1))
    put_price(world, 1, valid_from=EPOCH)                                           # the earliest day
    r = put_price(world, 1, valid_from=date(1999, 12, 31), expect=422)
    assert "слишком далеко в прошлом" in r["detail"]
    r = put_price(world, 1, valid_from=day(367), expect=422)
    assert "слишком далеко в будущем" in r["detail"]
    put_price(world, 1, valid_from=day(366))
    put_price(world, 1, note="  ", valid_from=day(2))                                # a blank note is no note
    assert group_rows(world)[-2][2] is None
    assert money_owner(world).put(f"{API}/org/groups/{uuid4()}/price", json={"amount": 1}).status_code == 404
    assert money_owner(world).get(f"{API}/org/groups/{uuid4()}/prices").status_code == 404


def test_the_dry_run_computes_the_impact_and_writes_nothing(world):
    put_price(world, 400_000, valid_from=day(-30))
    before = group_rows(world)
    out = put_price(world, 999_000, valid_from=day(-5), dry=True)
    assert out["dry_run"] is True and out["current"] is None and out["history"] is None
    assert out["impact"] == {"students_following": 3, "students_with_override": 0, "stale_payroll_sheets": 0, "stale_sheets": []}
    assert group_rows(world) == before


def test_the_impact_counts_followers_exceptions_and_ignores_archived_and_left_students(world):
    s0, s1, s2 = world.students
    put_override(world, s0, amount=300_000, reason="скидка")
    put_override(world, s1, amount=None, reason=None)                                # "the group price again": a follower
    extra = make_student(world.db, world.org, world.group, "Левый", "Ученик", "N13-777")
    world.db.commit()
    world.db.query(StudentGroup).filter_by(student_id=extra.id).update({"is_active": False, "left_at": datetime.utcnow()})
    gone = make_student(world.db, world.org, world.group, "Архивный", "Ученик", "N13-778")
    world.db.query(Student).filter_by(id=gone.id).update({"status": "archived"})
    world.db.commit()
    impact = put_price(world, 500_000, dry=True)["impact"]
    assert (impact["students_following"], impact["students_with_override"]) == (2, 1)
    # an exception that starts in the FUTURE is not one on the day the change reaches the student
    put_override(world, s2, amount=100_000, reason="с нового сезона", valid_from=day(40))
    assert put_price(world, 500_000, dry=True)["impact"]["students_with_override"] == 1
    assert put_price(world, 500_000, valid_from=day(41), dry=True)["impact"]["students_with_override"] == 2


# ============================================================================================
# the effective price: a group price, an exception, the way back
# ============================================================================================


def test_the_row_of_a_membership_carries_the_price_object_and_the_old_field_as_a_synonym(world):
    s0 = world.students[0]
    row = row_of(world, s0)
    assert row["price"] == {"amount": None, "source": "none", "valid_from": None, "group_amount": None, "reason": None}
    assert row["monthly_fee"] is None and row["status"] == "none"
    put_price(world, 450_000, valid_from=day(-20))
    row = row_of(world, s0)
    assert row["price"] == {"amount": 450_000, "source": "group", "valid_from": day(-20).isoformat(),
                            "group_amount": 450_000, "reason": None}
    assert row["monthly_fee"] == 450_000                                            # today's effective price, for the old front
    put_override(world, s0, amount=300_000, reason="многодетная семья")
    row = row_of(world, s0)
    assert row["price"] == {"amount": 300_000, "source": "override", "valid_from": business_today().isoformat(),
                            "group_amount": 450_000, "reason": "многодетная семья"}
    assert row["monthly_fee"] == 300_000
    other = row_of(world, world.students[1])
    assert other["price"]["source"] == "group" and other["monthly_fee"] == 450_000    # a neighbour is not touched
    # the same object in the status list and after "back to the group price"
    listed = next(i for i in money_owner(world).get(f"{API}/org/payments/status").json()["items"] if i["student_id"] == str(s0.id))
    assert listed["price"]["source"] == "override"
    out = put_override(world, s0, amount=None, reason=None)
    assert out["price"] == {"amount": 450_000, "source": "group", "valid_from": day(-20).isoformat(),
                            "group_amount": 450_000, "reason": None}
    assert out["row"]["price"]["source"] == "group" and out["row"]["monthly_fee"] == 450_000


def test_an_individual_price_needs_a_reason_and_a_sane_sum_the_way_back_does_not(world):
    s0 = world.students[0]
    for body in ({"amount": 1}, {"amount": 1, "reason": ""}, {"amount": 1, "reason": "  "}, {"amount": 1, "reason": "аб"},
                 {"amount": -1, "reason": "скидка"}, {"amount": 10_000_000_001, "reason": "скидка"},
                 {"amount": 1, "reason": "я" * 201}, {"amount": 1, "reason": "скидка", "valid_from": "1999-12-31"},
                 {"amount": 1, "reason": "скидка", "valid_from": day(367).isoformat()}):
        r = money_owner(world).put(f"{API}/org/memberships/{mid(world, s0)}/price-override", json=body)
        assert r.status_code == 422, body
    r = money_owner(world).put(f"{API}/org/memberships/{mid(world, s0)}/price-override", json={"amount": 1})
    assert r.json()["detail"]["code"] == "REASON_REQUIRED" and "причину" in r.json()["detail"]["message"]
    world.db.expire_all()
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=mid(world, s0)).count() == 0
    put_override(world, s0, amount=0, reason="грант")                                  # free is a price
    put_override(world, s0, amount=None, reason=None, valid_from=day(5))               # the way back needs no reason
    history = get_override(world, s0)["history"]
    assert [(h["amount"], h["reason"], h["is_current"], h["is_future"]) for h in history] == [
        (None, None, False, True), (0, "грант", True, False)]
    done = put_override(world, s0, amount=7, reason="  лишние пробелы  ")["history"]
    assert [h["reason"] for h in done if h["valid_from"] == business_today().isoformat()] == ["лишние пробелы"]


def test_the_same_day_again_corrects_the_exception_and_the_history_is_newest_first(world):
    s0 = world.students[0]
    put_override(world, s0, amount=300_000, reason="первая", valid_from=day(-30))
    put_override(world, s0, amount=250_000, reason="вторая", valid_from=day(-10))
    put_override(world, s0, amount=240_000, reason="вторая, исправлено", valid_from=day(-10))
    got = get_override(world, s0)
    assert [(h["amount"], h["reason"]) for h in got["history"]] == [(240_000, "вторая, исправлено"), (300_000, "первая")]
    assert got["price"]["amount"] == 240_000 and got["history"][0]["created_by_name"] == "Иван Owner"


def test_an_archived_student_gets_no_exception_but_it_can_be_read(world):
    s0 = world.students[0]
    put_override(world, s0, amount=300_000, reason="скидка")
    world.db.query(Student).filter_by(id=s0.id).update({"status": "archived"})
    world.db.commit()
    r = put_override(world, s0, amount=1, reason="ещё", expect=409)
    assert r["detail"] == "Ученик в архиве — сначала верните его"
    assert get_override(world, s0)["price"]["amount"] == 300_000


def test_a_group_price_may_be_set_for_a_group_without_students(world):
    empty = make_group(world.db, world.org, world.teacher2, "Пустая")
    world.db.commit()
    out = put_price(world, 100_000, group=empty)
    assert out["impact"] == {"students_following": 0, "students_with_override": 0, "stale_payroll_sheets": 0, "stale_sheets": []}


# ============================================================================================
# payments: the status, the hint, the soft warning - and the payments themselves never move
# ============================================================================================


def test_the_status_none_free_follows_the_price_of_the_group(world):
    assert {r["status"] for r in money_owner(world).get(f"{API}/org/payments/status").json()["items"]} == {"none"}
    put_price(world, 0)                                                              # a free group
    got = money_owner(world).get(f"{API}/org/payments/status").json()
    assert {r["status"] for r in got["items"]} == {"free"} and {r["monthly_fee"] for r in got["items"]} == {0}
    put_price(world, 450_000, valid_from=day(1))                                     # tomorrow: today it is still free
    assert row_of(world)["status"] == "free"
    put_override(world, world.students[0], amount=300_000, reason="скидка")           # an exception beats the free group
    assert row_of(world, world.students[0])["status"] == "none"                       # no payment, a price: «нет оплаты»
    assert money_owner(world).get(f"{API}/org/payments/summary").json()["free"] == 2


def test_the_hint_uses_the_price_at_the_start_of_the_paid_period(world):
    setup_mwf(world, fee=None, anchor="payment_date")
    give_group_price(world.db, world.group, 400_000, EPOCH)
    give_group_price(world.db, world.group, 500_000, D(2026, 3, 15))
    early = quote(world, paid_on=D(2026, 3, 5))                                      # 05.03 - 04.04: starts before the rise
    assert (early["suggested_amount"], early["monthly_fee"], early["price"]["amount"], early["price"]["source"]) == (
        400_000, 400_000, 400_000, "group")
    late = quote(world, paid_on=D(2026, 3, 16))
    assert (late["suggested_amount"], late["price"]["valid_from"]) == (500_000, "2026-03-15")
    typed = quote(world, paid_on=D(2026, 3, 16), monthly_fee=520_000)                # a typed sum still wins, once
    assert (typed["suggested_amount"], typed["monthly_fee"], typed["price"]["amount"]) == (520_000, 520_000, 500_000)
    # an exception beats the group price - from its day
    give_price(world.db, world.db.get(StudentGroup, mid(world)), 350_000, reason="скидка", valid_from=D(2026, 3, 10))
    assert quote(world, paid_on=D(2026, 3, 5))["suggested_amount"] == 400_000
    assert quote(world, paid_on=D(2026, 3, 16))["suggested_amount"] == 350_000
    # before the first price: no amount, the link to the group price is the front's business (price.source none)
    give_group_price(world.db, world.group, None)
    give_price(world.db, world.db.get(StudentGroup, mid(world)), None)
    none = quote(world, paid_on=D(2026, 3, 5))
    assert (none["suggested_amount"], none["basis"], none["price"]["source"]) == (None, "none", "none")
    assert none["explanation"] == "Оплата в месяц не задана"


def test_the_hint_of_a_package_is_the_price_of_the_package(world):
    setup_mwf(world, fee=None)
    give_group_price(world.db, world.group, 600_000, EPOCH)
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    q = quote(world, paid_on=D(2026, 3, 5))
    assert (q["basis"], q["suggested_amount"]) == ("package", 600_000)
    assert quote(world, paid_on=D(2026, 3, 5), lessons_count=6)["suggested_amount"] == 300_000


def test_the_soft_warning_compares_with_the_price_at_the_start_of_the_period(world):
    give_group_price(world.db, world.group, 450_000, EPOCH)
    start = day(-3)
    r = money_owner(world).post(f"{API}/org/memberships/{mid(world)}/payments", json={
        "amount": 450_000, "paid_on": day().isoformat(), "valid_from": start.isoformat(), "valid_to": day(1).isoformat()})
    assert r.status_code == 201
    assert "short_period_full_amount" in [w["code"] for w in r.json()["warnings"]]       # 5 days for the full price
    give_group_price(world.db, world.group, 900_000, start + timedelta(days=1))          # the price rose AFTER the start
    r = money_owner(world).post(f"{API}/org/memberships/{mid(world, world.students[1])}/payments", json={
        "amount": 450_000, "paid_on": day().isoformat(), "valid_from": start.isoformat(), "valid_to": day(1).isoformat()})
    assert "short_period_full_amount" in [w["code"] for w in r.json()["warnings"]]       # still judged at the price of the start


def test_a_backdated_price_never_alters_a_payment_already_entered(world):
    give_group_price(world.db, world.group, 400_000, EPOCH)
    made = pay(world, amount=400_000, paid_on=day(-20), valid_from=day(-20), valid_to=day(9))["payment"]
    second = pay(world, world.students[1], amount=123_456, paid_on=day(-5), valid_from=day(-5), valid_to=day(24))["payment"]
    history = money_owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json()
    stored = [(p.id, p.amount, p.valid_from, p.valid_to, p.voided_at, p.receipt_no, p.created_at) for p in
              world.db.query(StudentPayment).filter_by(org_id=world.org.id).order_by(StudentPayment.created_at)]
    put_price(world, 999_000, valid_from=day(-40))                                   # far in the past, far above
    put_price(world, 1, valid_from=day(-19))
    put_override(world, world.students[1], amount=5, reason="проверка", valid_from=day(-30))
    world.db.expire_all()
    assert [(p.id, p.amount, p.valid_from, p.valid_to, p.voided_at, p.receipt_no, p.created_at) for p in
            world.db.query(StudentPayment).filter_by(org_id=world.org.id).order_by(StudentPayment.created_at)] == stored
    assert money_owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json() == history
    assert money_owner(world).get(f"{API}/org/payments/{made['id']}/receipt").json()["amount"] == 400_000
    assert money_owner(world).get(f"{API}/org/payments/{second['id']}/receipt").json()["amount"] == 123_456


# ============================================================================================
# payroll: the price of EACH lesson
# ============================================================================================


def test_a_price_raised_from_the_15th_splits_the_month_by_lesson_date(world):
    m, (s0, s1, s2) = previous_month(), world.students
    give_group_price(world.db, world.group, 400_000, EPOCH)
    give_group_price(world.db, world.group, 500_000, m.d(15))
    month_lessons(world, m, (10, 12, 15, 17))
    paid_month(world, m)
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    # two lessons at 400 000 and two at 500 000: (2 * 400 000 + 2 * 500 000) * 50 % / 12 = 75 000
    assert line["amount"] == (2 * 400_000 + 2 * 500_000) * 50 // 1200 == 75_000
    assert [(x["date"], x["price"], x["price_source"]) for x in line["marks"]] == [
        (m.d(10).isoformat(), 400_000, "group"), (m.d(12).isoformat(), 400_000, "group"),
        (m.d(15).isoformat(), 500_000, "group"), (m.d(17).isoformat(), 500_000, "group")]
    assert line["monthly_fee"] == 500_000 and line["source_fee"] == 500_000 and line["price_source"] == "group"
    assert line["price_changed"] is True
    assert line["price_steps"] == [{"date": m.d(10).isoformat(), "amount": 400_000}, {"date": m.d(15).isoformat(), "amount": 500_000}]
    assert sheet["total_amount"] == 3 * 75_000
    # neither the old price for the whole month nor the new one for the whole month
    assert line["amount"] not in (4 * 400_000 * 50 // 1200, 4 * 500_000 * 50 // 1200)
    # the saved sheet and its Excel say so as well
    saved = save_sheet(world, m)
    assert line_of(saved, s0)["price_steps"] == line["price_steps"] and saved["stale"] is False
    ws = workbook(money_owner_get(world, f"{API}/org/payroll/sheets/{saved['id']}/export.xlsx")).active
    cell = next(c for c in ws["E"] if c.value == 500_000)
    assert "Цена менялась в этом месяце" in cell.comment.text and "с 15." in cell.comment.text
    assert cell.fill.start_color.rgb.endswith("FFF2B3")


def money_owner_get(w, url):
    return money_owner(w).get(url)


def test_an_individual_price_applies_from_its_day_and_the_way_back_too(world):
    m, (s0, s1, s2) = previous_month(), world.students
    give_group_price(world.db, world.group, 400_000, EPOCH)
    give_price(world.db, world.db.get(StudentGroup, mid(world, s0)), 300_000, reason="скидка", valid_from=m.d(11))
    month_lessons(world, m, (10, 12, 15, 17))
    paid_month(world, m)
    line = line_of(preview(world, m), s0)
    assert [x["price"] for x in line["marks"]] == [400_000, 300_000, 300_000, 300_000]
    assert line["amount"] == 54_167 and line["price_source"] == "override"
    # back to the group price from the 15th: lessons 15 and 17 at 400 000 again
    world.db.add(MembershipPriceOverride(membership_id=mid(world, s0), amount=None, valid_from=m.d(15), created_at=datetime.utcnow()))
    world.db.commit()
    line = line_of(preview(world, m), s0)
    assert [(x["price"], x["price_source"]) for x in line["marks"]] == [
        (400_000, "group"), (300_000, "override"), (400_000, "group"), (400_000, "group")]
    assert line["amount"] == 62_500
    assert line_of(preview(world, m), s1)["amount"] == 66_667            # a neighbour: the plain group price
    assert line_of(preview(world, m), s1)["price_changed"] is False and line_of(preview(world, m), s1)["price_steps"] == []


def test_lessons_before_the_first_price_have_none_and_the_sheet_says_so(world):
    m, (s0, s1, s2) = previous_month(), world.students
    give_group_price(world.db, world.group, 400_000, m.d(15))
    month_lessons(world, m, (10, 12, 15, 17))
    paid_month(world, m)
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    assert [x["price"] for x in line["marks"]] == [None, None, 400_000, 400_000]
    assert line["amount"] == 2 * 400_000 * 50 // 1200 and line["no_price_lessons"] == 2
    assert [w["code"] for w in line["warnings"]] == ["NO_FEE"]
    assert sheet["warnings"]["no_fee"] == 3 and overview(world, m)["teachers"][0]["warnings"]["no_fee"] == 3


def test_the_owner_overwrite_on_the_sheet_replaces_the_price_of_every_lesson_and_the_reset_brings_the_history_back(world):
    m, (s0, s1, s2) = previous_month(), world.students
    give_group_price(world.db, world.group, 400_000, EPOCH)
    give_group_price(world.db, world.group, 500_000, m.d(15))
    month_lessons(world, m, (10, 12, 15, 17))
    paid_month(world, m)
    saved = save_sheet(world, m, line_overrides=[{"membership_id": line_of(preview(world, m), s0)["membership_id"], "monthly_fee": 300_000}])
    line = line_of(saved, s0)
    assert line["fee_overridden"] is True and line["monthly_fee"] == 300_000 and line["price_changed"] is False
    assert line["amount"] == 4 * 300_000 * 50 // 1200
    r = money_owner(world).patch(f"{API}/org/payroll/sheets/{saved['id']}", json={
        "revision": 1, "line_overrides": [{"membership_id": line["membership_id"], "monthly_fee": None}]})
    back = line_of(r.json(), s0)
    assert back["fee_overridden"] is False and back["amount"] == 75_000 and back["price_changed"] is True


def test_a_free_period_of_the_price_is_not_unpaid_news(world):
    m, (s0, s1, s2) = previous_month(), world.students
    give_group_price(world.db, world.group, 0, EPOCH)
    give_group_price(world.db, world.group, 400_000, m.d(15))
    month_lessons(world, m, (10, 12, 15, 17), students=[s0])
    pay_calendar(world, s0, m.first, m.d(11), fee=None)                              # 10th paid, the rest not
    line = line_of(preview(world, m), s0)
    assert [x["state"] for x in line["marks"]] == ["counted", "unpaid", "unpaid", "unpaid"]
    assert line["unpaid"] == 3 and line["unpaid_due"] == 2                           # the 12th is free: nobody owes it
    assert [w["message"] for w in line["warnings"] if w["code"] == "UNPAID_LESSONS"] == ["Не оплачено уроков: 2"]
    ticked = save_sheet(world, m, line_overrides=[{"membership_id": line["membership_id"], "include_unpaid": True}])
    assert line_of(ticked, s0)["amount"] == (0 + 0 + 400_000 + 400_000) * 50 // 1200


def test_the_whole_chain_group_price_then_payment_then_payroll_with_a_rise_in_the_middle_of_the_month(world):
    """The owner's flow through the API only: set the prices, take the hint, enter the payment, hold the lessons,
    calculate the payroll; then the price is changed backdated: the payment stays, the sheet turns stale."""
    m, (s0, s1, s2) = previous_month(), world.students
    put_price(world, 400_000, valid_from=EPOCH, note="до повышения")
    put_price(world, 500_000, valid_from=m.d(15), note="повышение с 15-го")
    hint = quote(world, membership_id=mid(world, s0), paid_on=m.first)                 # the period 1st .. last of the month
    assert (hint["valid_from"], hint["valid_to"], hint["suggested_amount"], hint["price"]["amount"]) == (
        m.first.isoformat(), m.last.isoformat(), 400_000, 400_000)                       # the price at the START of the period
    made = pay(world, s0, amount=hint["suggested_amount"], paid_on=m.first.isoformat(), valid_from=hint["valid_from"],
               valid_to=hint["valid_to"])
    assert made["payment"]["amount"] == 400_000
    for other in (s1, s2):
        pay(world, other, amount=400_000, paid_on=m.first.isoformat(), valid_from=hint["valid_from"], valid_to=hint["valid_to"])
    month_lessons(world, m, (10, 12, 15, 17))
    sheet = preview(world, m)
    assert [line_of(sheet, s)["amount"] for s in world.students] == [75_000] * 3 and sheet["total_amount"] == 225_000
    saved = save_sheet(world, m)
    # a rise from the 11th: the payments are what they were, the sheet says "данные изменились", the recalculation uses the new prices
    changed = put_price(world, 450_000, valid_from=m.d(11), note="пересмотр задним числом")
    assert changed["impact"]["stale_payroll_sheets"] == 1 and changed["impact"]["students_following"] == 3
    assert money_owner(world).get(f"{API}/org/memberships/{mid(world, s0)}/payments").json()[0]["amount"] == 400_000
    assert sheet_state(world, m, saved)["stale"] is True and saved["total_amount"] == 225_000
    fresh = preview(world, m)
    # 10th at 400 000; 12th and 15th at 450 000 (the 11th raise); 17th at 500 000 (the 15th rise stays)
    assert [x["price"] for x in line_of(fresh, s0)["marks"]] == [400_000, 450_000, 500_000, 500_000]
    assert line_of(fresh, s0)["amount"] == 77_083                                     # 1 850 000 * 50 % / 12 = 77 083.33
    assert [h["amount"] for h in get_prices(world)["history"]] == [500_000, 450_000, 400_000]


# ============================================================================================
# payroll: the fingerprint, stale sheets, the impact
# ============================================================================================


def saved_flat_month(w, price=400_000):
    """A saved sheet of the previous month, a price that never changed; lessons on the 10th, 12th, 15th and 17th."""
    m = previous_month()
    give_group_price(w.db, w.group, price, EPOCH)
    month_lessons(w, m, (10, 12, 15, 17))
    paid_month(w, m)
    return m, save_sheet(w, m)


def sheet_state(w, m, sheet):
    got = money_owner(w).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()
    ov = teacher_row(overview(w, m), w.teacher)["sheet"]
    listed = next(x for x in money_owner(w).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json() if x["id"] == sheet["id"])
    assert got["stale"] == ov["stale"] == listed["stale"]
    return got


def test_a_price_set_backdated_marks_the_saved_sheet_stale_and_the_sheet_itself_does_not_change(world):
    m, saved = saved_flat_month(world)
    assert sheet_state(world, m, saved)["stale"] is False and saved["total_amount"] == 3 * 66_667
    fingerprint = saved["source_fingerprint"]
    impact = put_price(world, 450_000, valid_from=m.d(11), dry=True)["impact"]
    assert (impact["stale_payroll_sheets"], impact["students_following"], impact["students_with_override"]) == (1, 3, 0)
    assert [(s["month"], s["teacher_id"], s["teacher_name"]) for s in impact["stale_sheets"]] == [
        (m.label, str(world.teacher.id), "Пётр Teacher")]
    assert sheet_state(world, m, saved)["stale"] is False                           # a dry run touches nothing
    out = put_price(world, 450_000, valid_from=m.d(11))
    assert out["impact"]["stale_payroll_sheets"] == 1
    now = sheet_state(world, m, saved)
    assert now["stale"] is True                                                      # «данные изменились»
    assert now["total_amount"] == saved["total_amount"] and now["source_fingerprint"] == fingerprint   # the saved sheet is what it was
    assert line_of(now, world.students[0])["amount"] == 66_667
    diff = money_owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    assert diff["stale"] is True and diff["fresh_total"] == 3 * 72_917 and diff["delta_amount"] == 3 * (72_917 - 66_667)
    # the preview has the new data fingerprint, the owner recalculates and the sheet is current again
    fresh = preview(world, m)
    assert fresh["source_fingerprint"] != fingerprint and line_of(fresh, world.students[0])["price_steps"] == [
        {"date": m.d(10).isoformat(), "amount": 400_000}, {"date": m.d(12).isoformat(), "amount": 450_000}]
    r = money_owner(world).patch(f"{API}/org/payroll/sheets/{saved['id']}", json={"revision": 1, "recalculate": True})
    assert r.status_code == 200 and r.json()["stale"] is False and r.json()["total_amount"] == 3 * 72_917


def test_a_price_that_touches_no_lesson_of_a_saved_sheet_leaves_it_current(world):
    m, saved = saved_flat_month(world)
    for valid_from in (m.d(20), business_today(), day(40)):                          # after the last lesson / this month / the future
        impact = put_price(world, 777_000, valid_from=valid_from, dry=True)["impact"]
        assert impact["stale_payroll_sheets"] == 0 and impact["stale_sheets"] == [], valid_from
    put_price(world, 777_000, valid_from=m.d(20))
    put_price(world, 888_000, valid_from=business_today())
    assert sheet_state(world, m, saved)["stale"] is False
    assert put_price(world, 400_000, valid_from=EPOCH, dry=True)["impact"]["stale_payroll_sheets"] == 0   # the same price again


def test_a_price_change_does_not_touch_a_student_who_has_an_individual_price_that_day(world):
    m, saved = saved_flat_month(world)
    for s in world.students:                                                          # everybody has an exception for the whole month
        give_price(world.db, world.db.get(StudentGroup, mid(world, s)), 380_000, reason="договор", valid_from=m.first)
    saved = save_sheet(world, m, revision=1)
    assert saved["stale"] is False
    impact = put_price(world, 450_000, valid_from=m.d(1), dry=True)["impact"]
    assert (impact["stale_payroll_sheets"], impact["students_following"], impact["students_with_override"]) == (0, 0, 3)
    put_price(world, 450_000, valid_from=m.d(1))
    assert sheet_state(world, m, saved)["stale"] is False                             # nobody's lesson was priced by the group
    # one of them goes back to the group price from the 12th: the sheet turns stale
    world.db.add(MembershipPriceOverride(membership_id=mid(world, world.students[0]), amount=None, valid_from=m.d(12), created_at=datetime.utcnow()))
    world.db.commit()
    assert sheet_state(world, m, saved)["stale"] is True


def test_an_exception_set_backdated_marks_the_sheet_stale_too(world):
    m, saved = saved_flat_month(world)
    put_override(world, world.students[0], amount=300_000, reason="пересмотр", valid_from=m.d(12))
    assert sheet_state(world, m, saved)["stale"] is True


def test_the_fingerprint_of_a_sheet_saved_before_the_group_price_stays_the_same_after_the_migration_of_the_fees(world):
    """A saved sheet hashes the fee of the line; after the migration the same number comes from the price, so no
    saved sheet turns «данные изменились» at the release."""
    m = previous_month()
    month_lessons(world, m, (10, 12))
    for s in world.students:
        pay_calendar(world, s, m.first, m.last, fee=450_000)                          # an individual price from "always"
    saved = save_sheet(world, m)
    fingerprint = saved["source_fingerprint"]
    for s in world.students:                                                          # the migrated shape: one group price, no exceptions
        give_price(world.db, world.db.get(StudentGroup, mid(world, s)), None)
    give_group_price(world.db, world.group, 450_000, EPOCH)
    assert preview(world, m)["source_fingerprint"] == fingerprint
    assert sheet_state(world, m, saved)["stale"] is False


def test_the_list_of_stale_sheets_is_for_the_owner_only(world):
    m, saved = saved_flat_month(world)
    allow_admin(world)
    admin = make_client(world, user=world.admin)
    got = put_price(world, 450_000, valid_from=m.d(11), dry=True, client=admin)["impact"]
    assert got["stale_payroll_sheets"] == 1 and got["stale_sheets"] == []              # the count yes, the sheets (payroll) no
    assert admin.get(f"{API}/org/payroll/sheets").status_code == 403                    # payroll itself stays the owner's alone
    assert put_price(world, 450_000, valid_from=m.d(11), dry=True)["impact"]["stale_sheets"] != []


def test_a_price_change_in_the_current_month_marks_nothing_that_was_not_saved(world):
    m = current_month()
    give_group_price(world.db, world.group, 400_000, EPOCH)
    assert put_price(world, 500_000, valid_from=m.first, dry=True)["impact"]["stale_payroll_sheets"] == 0


# ============================================================================================
# no N+1
# ============================================================================================


def build_priced_centre(w, students_per):
    """One teacher, one group of ``students_per`` students with a price history, exceptions and lessons."""
    m = previous_month()
    teacher = make_user(w.db, "teacher", "Учитель")
    make_member(w.db, w.org, teacher, "teacher")
    group = make_group(w.db, w.org, teacher, "Группа цен")
    pupils = [make_student(w.db, w.org, group, f"Имя{i:03d}", f"Фам{i:03d}") for i in range(students_per)]
    w.db.flush()
    memberships = {x.student_id: x for x in w.db.query(StudentGroup).filter(StudentGroup.group_id == group.id)}
    for n in (3, 5, 8, 10, 12, 15, 17, 19):
        w.db.add(GroupAttendance(
            org_id=w.org.id, group_id=group.id, lesson_date=m.d(n).isoformat(), lesson_id=f"s{m.d(n).isoweekday()}",
            status="completed", records={str(s.id): "present" for s in pupils},
            roster_snapshot=[{"student_id": str(s.id)} for s in pupils], teacher_id=teacher.id))
    w.db.add(GroupPrice(group_id=group.id, amount=400_000, valid_from=EPOCH, created_at=datetime.utcnow()))
    w.db.add(GroupPrice(group_id=group.id, amount=500_000, valid_from=m.d(15), created_at=datetime.utcnow()))
    for i, s in enumerate(pupils):
        membership = memberships[s.id]
        if i % 4 == 0:
            w.db.add(MembershipPriceOverride(membership_id=membership.id, amount=300_000 + i, valid_from=m.d(1),
                                             reason="договорённость", created_at=datetime.utcnow()))
        if i % 4 == 1:
            w.db.add(MembershipPriceOverride(membership_id=membership.id, amount=None, valid_from=m.d(12), created_at=datetime.utcnow()))
        w.db.add(StudentPayment(org_id=w.org.id, membership_id=membership.id, student_id=s.id, group_id=group.id, amount=1,
                                paid_on=m.first, valid_from=m.first, valid_to=m.last, created_by=w.owner.id))
    w.db.commit()
    return m, [{"teacher_id": str(teacher.id), "lesson_type": "group", "percent": "40"}], teacher


def test_a_month_of_30_students_with_price_history_costs_the_same_queries_as_one_of_3(world):
    small_m, small_percents, _ = build_priced_centre(world, 3)
    client = money_owner(world)
    q_small, r = count_queries(world, lambda: client.post(f"{API}/org/payroll/months/{small_m.label}/calculate", json={"percents": small_percents}))
    assert r.status_code == 200, r.text
    q_overview_small, _ = count_queries(world, lambda: client.get(f"{API}/org/payroll/months/{small_m.label}"))
    org = Organization(name="Big priced centre", status="active")
    world.db.add(org)
    world.db.flush()
    owner2 = make_user(world.db, "owner")
    make_member(world.db, org, owner2, "owner")
    world.db.commit()
    big = SimpleNamespace(**{**vars(world), "org": org, "owner": owner2})
    m, percents, teacher = build_priced_centre(big, 30)
    client2 = make_client(big, user=owner2)
    q_big, resp = count_queries(world, lambda: client2.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": percents}))
    assert resp.status_code == 200, resp.text
    (sheet,) = [s for s in resp.json()["sheets"] if s["teacher_id"] == percents[0]["teacher_id"]]
    assert len(sheet["lines"]) == 30 and any(line["price_changed"] for line in sheet["lines"]) and sheet["total_amount"] > 0
    assert {line["price_source"] for line in sheet["lines"]} == {"group", "override"}
    q_overview_big, ov = count_queries(world, lambda: client2.get(f"{API}/org/payroll/months/{m.label}"))
    assert ov.status_code == 200
    assert q_big == q_small and q_overview_big == q_overview_small, (q_small, q_big, q_overview_small, q_overview_big)
    assert q_big <= 45 and q_overview_big <= 45
    # the price pages of the whole group list: a fixed number of queries as well
    q_pages_small, _ = count_queries(world, lambda: client.get(f"{API}/org/payments/status"))
    q_pages_big, listed = count_queries(world, lambda: client2.get(f"{API}/org/payments/status", params={"limit": 100}))
    assert listed.json()["total"] >= 30 and q_pages_big == q_pages_small, (q_pages_small, q_pages_big)
    q_groups_small, _ = count_queries(world, lambda: client.get(f"{API}/org/groups"))
    assert q_groups_small <= 12


def test_the_batch_loader_is_two_queries_for_any_number_of_memberships_and_days(world):
    m, percents, teacher = build_priced_centre(world, 30)
    pairs = [(x.id, x.group_id) for x in world.db.query(StudentGroup).limit(60)]
    n, books = count_queries(world, lambda: pricing.load_books(world.db, {g for _, g in pairs}, [i for i, _ in pairs]))
    assert n == 2 and len(pairs) == 33 or n == 2
    n, _ = count_queries(world, lambda: [books.price(i, g, m.d(d)) for i, g in pairs for d in range(1, 29)])
    assert n == 0                                                                    # any number of days: no query


# ============================================================================================
# who sees the price
# ============================================================================================


def test_the_group_list_and_card_carry_the_price_for_the_money_roles_only(world):
    put_price(world, 450_000, valid_from=day(-20), note="секрет")
    secret = "450000"
    owner_list = money_owner(world).get(f"{API}/org/groups").json()
    mine = next(g for g in owner_list if g["id"] == str(world.group.id))
    assert mine["price_current"] == {"amount": 450_000, "valid_from": day(-20).isoformat()}
    other = next(g for g in owner_list if g["id"] != str(world.group.id)) if len(owner_list) > 1 else None
    if other:
        assert other["price_current"] is None                                          # a group without a price: null, the key is there
    card = money_owner(world).get(f"{API}/org/groups/{world.group.id}").json()
    assert card["price_current"]["amount"] == 450_000
    # an admin without the right, and a teacher: the key is not there, the number is nowhere in the answer
    for user in (world.admin, world.teacher):
        client = make_client(world, user=user)
        for url in (f"{API}/org/groups", f"{API}/org/groups/{world.group.id}", f"{API}/org/groups/{world.group.id}/stats",
                    f"{API}/org/students", f"{API}/org/students/{world.students[0].id}", f"{API}/org/students/{world.students[0].id}/profile",
                    f"{API}/org/groups-overview", f"{API}/me", f"{API}/org"):
            r = client.get(url)
            if r.status_code != 200:
                assert r.status_code in (403, 404, 422), (user.role, url)
                continue
            assert "price" not in r.text and secret not in r.text and "секрет" not in r.text, (user.role, url)
        for url in (f"{API}/org/groups/{world.group.id}/prices", f"{API}/org/memberships/{mid(world)}/price-override",
                    f"{API}/org/payments/status"):
            assert client.get(url).status_code == 403
        assert client.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 1}).status_code == 403
        assert client.put(f"{API}/org/groups/{world.group.id}/price?dry_run=1", json={"amount": 1}).status_code == 403
        assert client.put(f"{API}/org/memberships/{mid(world)}/price-override", json={"amount": 1, "reason": "проверка"}).status_code == 403
        assert client.put(f"{API}/org/memberships/{mid(world)}/fee", json={"monthly_fee": 1}).status_code == 403
    # ... and with the right the admin sees it
    allow_admin(world)
    admin_list = make_client(world, user=world.admin).get(f"{API}/org/groups").json()
    assert next(g for g in admin_list if g["id"] == str(world.group.id))["price_current"]["amount"] == 450_000
    assert make_client(world, user=world.admin).get(f"{API}/org/groups/{world.group.id}").json()["price_current"]["amount"] == 450_000
    # the teacher still does not, with the switch on
    assert "price_current" not in make_client(world, user=world.teacher).get(f"{API}/org/groups").text


def test_the_teacher_does_not_see_a_price_in_the_roster_the_journal_or_the_profile_of_a_priced_student(world):
    put_price(world, 7_654_321, valid_from=day(-20))
    put_override(world, world.students[0], amount=1_234_567, reason="личное")
    client = make_client(world, user=world.teacher)
    for url in (f"{API}/org/groups/{world.group.id}", f"{API}/org/students/{world.students[0].id}/profile",
                f"{API}/org/groups/{world.group.id}/attendance?date_from=2026-01-01&date_to=2026-01-31", f"{API}/org/students"):
        r = client.get(url)
        assert "7654321" not in r.text and "1234567" not in r.text and "личное" not in r.text, url


# ============================================================================================
# locks and errors
# ============================================================================================


def test_a_group_price_write_locks_only_the_group_row_for_no_key_update(world):
    from sqlalchemy import event

    statements = []
    engine_ = world.db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine_, "before_cursor_execute", record)
    try:
        put_price(world, 450_000)
        put_override(world, world.students[0])
    finally:
        event.remove(engine_, "before_cursor_execute", record)
    locking = [s for s in statements if " FOR " in s]
    assert any("FROM groups" in s and "FOR NO KEY UPDATE" in s for s in locking), locking
    assert any("FROM student_groups" in s and "FOR NO KEY UPDATE" in s for s in locking), locking
    assert all("FOR NO KEY UPDATE" in s for s in locking), [s for s in locking if "FOR NO KEY UPDATE" not in s]


def test_a_price_write_waits_for_a_holder_of_the_group_but_not_for_an_insert_that_points_at_it(world, engine):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    holder = Session(engine)
    holder.execute(text("SELECT id FROM groups WHERE id = :g FOR NO KEY UPDATE"), {"g": world.group.id})
    thread, outcome = run_in_thread(engine, lambda session: pricing.PricingService(session).set_group_price(
        str(world.org.id), world.group.id, str(world.owner.id), amount=111_000, valid_from=day(-3)))
    wait_until_blocked(engine)                                                       # the price write waits for the holder ...
    with Session(engine) as other:                                                   # ... an insert that points at the group (KEY SHARE) does not
        other.execute(text("SET LOCAL lock_timeout = '2s'"))
        other.add(GroupAttendance(org_id=world.org.id, group_id=world.group.id, lesson_date="2026-01-05", lesson_id="x1",
                                  status="cancelled", records={}, roster_snapshot=[], cancellation_reason="тест"))
        other.commit()
    assert thread.is_alive()
    holder.rollback()
    holder.close()
    thread.join(10)
    assert "error" not in outcome, outcome.get("error")
    assert group_rows(world) == [(day(-3), 111_000, None)]


def finish(thread, outcome):
    thread.join(15)
    assert not thread.is_alive()
    return outcome


def test_two_concurrent_writes_of_the_same_day_end_clean_one_wins_the_other_is_a_409_or_a_correction(world, engine):
    """Whichever order the lock gives them: no 500, one row, and the loser is told (409) or - when it began after the
    winner had written - has simply corrected the day."""
    results = []
    for amount in (111_000, 222_000):
        results.append(run_in_thread(engine, lambda session, a=amount: pricing.PricingService(session).set_group_price(
            str(world.org.id), world.group.id, str(world.owner.id), amount=a, valid_from=day(-3))))
    outcomes = [finish(*r) for r in results]
    errors = [o["error"] for o in outcomes if "error" in o]
    assert len(errors) <= 1 and all(isinstance(e, pricing.PriceConflict) and e.status_code == 409 for e in errors), errors
    rows = group_rows(world)
    assert len(rows) == 1 and rows[0][1] in (111_000, 222_000)
    mids = mid(world)
    results = []
    for amount in (10_000, 20_000):
        results.append(run_in_thread(engine, lambda session, a=amount: pricing.PricingService(session).set_override(
            str(world.org.id), mids, str(world.owner.id), amount=a, valid_from=day(-3), reason="гонка")))
    outcomes = [finish(*r) for r in results]
    errors = [o["error"] for o in outcomes if "error" in o]
    assert len(errors) <= 1 and all(isinstance(e, pricing.PriceConflict) for e in errors), errors
    world.db.expire_all()
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=mids).count() == 1


def written_while_waiting(world, engine, run, lock_sql, write):
    """``run(session)`` is the request; ``lock_sql`` is held by a holder; the request waits; the holder writes the
    very row the request is about to write and lets go."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    holder = Session(engine)
    holder.execute(text(lock_sql[0]), lock_sql[1])
    thread, outcome = run_in_thread(engine, run)
    wait_until_blocked(engine)
    write(holder)
    holder.commit()
    holder.close()
    return finish(thread, outcome)


def test_a_group_price_written_by_somebody_else_while_this_request_waited_is_a_409_not_an_overwrite(world, engine):
    def somebody_else(holder):
        holder.add(GroupPrice(group_id=world.group.id, amount=111_000, valid_from=day(-3), note="чужая",
                              created_by=world.admin.id, created_at=datetime.utcnow()))

    def request(amount, note=None):
        return lambda session: pricing.PricingService(session).set_group_price(
            str(world.org.id), world.group.id, str(world.owner.id), amount=amount, valid_from=day(-3), note=note)

    lock = ("SELECT id FROM groups WHERE id = :g FOR NO KEY UPDATE", {"g": world.group.id})
    out = written_while_waiting(world, engine, request(222_000), lock, somebody_else)
    assert isinstance(out.get("error"), pricing.PriceConflict)
    assert out["error"].detail == {"code": "PRICE_CONFLICT", "message": "Цену только что изменили. Обновите страницу и повторите."}
    assert group_rows(world) == [(day(-3), 111_000, "чужая")]                          # the other write stands
    # the same request twice (a double click) is done, not refused
    world.db.query(GroupPrice).delete()
    world.db.commit()
    out = written_while_waiting(world, engine, request(111_000, "чужая"), lock, somebody_else)
    assert "error" not in out, out.get("error")
    assert group_rows(world) == [(day(-3), 111_000, "чужая")]
    # a deliberate correction of a row that existed when the request was made is an update
    out = finish(*run_in_thread(engine, request(333_000, "исправлено")))
    assert "error" not in out and group_rows(world) == [(day(-3), 333_000, "исправлено")]


def test_an_exception_written_by_somebody_else_while_this_request_waited_is_a_409_not_an_overwrite(world, engine):
    membership_id = mid(world)

    def somebody_else(holder):
        holder.add(MembershipPriceOverride(membership_id=membership_id, amount=10_000, valid_from=day(-3), reason="чужая",
                                           created_by=world.admin.id, created_at=datetime.utcnow()))

    def request(amount):
        return lambda session: pricing.PricingService(session).set_override(
            str(world.org.id), membership_id, str(world.owner.id), amount=amount, valid_from=day(-3), reason="чужая")

    lock = ("SELECT id FROM student_groups WHERE id = :m FOR NO KEY UPDATE", {"m": membership_id})
    out = written_while_waiting(world, engine, request(20_000), lock, somebody_else)
    assert isinstance(out.get("error"), pricing.PriceConflict) and out["error"].status_code == 409
    world.db.expire_all()
    assert [(r.amount, r.reason) for r in world.db.query(MembershipPriceOverride).filter_by(membership_id=membership_id)] == [(10_000, "чужая")]
    out = finish(*run_in_thread(engine, request(30_000)))                              # a later request corrects the day
    assert "error" not in out
    world.db.expire_all()
    assert [r.amount for r in world.db.query(MembershipPriceOverride).filter_by(membership_id=membership_id)] == [30_000]


def test_a_collision_that_slips_through_is_a_clean_409_never_a_500(world, monkeypatch):
    client = money_owner(world)
    original = world.db.commit

    def collide():
        world.db.rollback()
        raise IntegrityError("INSERT INTO group_prices", {}, Exception("duplicate key value violates unique constraint"))

    monkeypatch.setattr(world.db, "commit", collide)
    r = client.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_CONFLICT" and "Обновите страницу" in r.json()["detail"]["message"]
    r = client.put(f"{API}/org/memberships/{mid(world)}/price-override", json={"amount": 1, "reason": "гонка"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_CONFLICT"
    deadlock = SimpleNamespace(pgcode="40P01")

    def dead():
        world.db.rollback()
        raise OperationalError("UPDATE", {}, deadlock)

    monkeypatch.setattr(world.db, "commit", dead)
    r = client.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_CONFLICT"
    monkeypatch.setattr(world.db, "commit", original)
    assert group_rows(world) == []                                                   # nothing was written
    assert put_price(world, 5)["current"]["amount"] == 5


def test_a_foreign_group_or_membership_is_a_404_and_nothing_is_written(world):
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="owner", role="owner")
    world.db.query(__import__("src.models.auth", fromlist=["OrganizationMember"]).OrganizationMember).filter_by(
        user_id=world.foreign_admin.id).update({"org_role": "owner"})
    world.db.commit()
    assert put_price(world, 1, client=foreign, expect=404)["detail"] == "Группа не найдена"
    assert get_prices(world, client=foreign, expect=404)
    assert put_price(world, 1, client=foreign, dry=True, expect=404)
    assert put_override(world, client=foreign, expect=404)["detail"] == "Членство не найдено"
    assert get_override(world, client=foreign, expect=404)
    assert group_rows(world) == []


# ============================================================================================
# the audit trail: every write of a price leaves a row in group_audit, in the same transaction
# ============================================================================================

PRICE_ACTIONS = (pricing.ACTION_GROUP_PRICE, pricing.ACTION_MEMBERSHIP_PRICE)


def audit_rows(w, action=None, group=None):
    """The price rows of ``group_audit`` of the group, oldest first."""
    w.db.expire_all()
    query = w.db.query(GroupAudit).filter(GroupAudit.group_id == (group or w.group).id,
                                          GroupAudit.action.in_([action] if action else PRICE_ACTIONS))
    return query.order_by(GroupAudit.created_at, GroupAudit.id).all()


def committed(engine, sql, **params):
    """A number read on its OWN connection: only what has been committed is there."""
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def committed_state(engine, w):
    """``(group price rows, override rows, price audit rows)`` of the group, committed."""
    return (
        committed(engine, "SELECT count(*) FROM group_prices WHERE group_id = :g", g=w.group.id),
        committed(engine, "SELECT count(*) FROM membership_price_overrides o JOIN student_groups sg ON sg.id = o.membership_id "
                          "WHERE sg.group_id = :g", g=w.group.id),
        committed(engine, "SELECT count(*) FROM group_audit WHERE group_id = :g AND action IN ('group_price_set', 'membership_price_set')",
                  g=w.group.id),
    )


def stamp_of_group_price(w):
    w.db.expire_all()
    return w.db.query(GroupPrice.id, GroupPrice.created_at, GroupPrice.created_by).filter_by(
        group_id=w.group.id, valid_from=day(-3)).one()


def stamp_of_override(w, student):
    w.db.expire_all()
    return w.db.query(MembershipPriceOverride.id, MembershipPriceOverride.created_at,
                      MembershipPriceOverride.created_by).filter_by(membership_id=mid(w, student), valid_from=day(-3)).one()


def trail(got, valid_from):
    return next(h for h in got["history"] if h["valid_from"] == valid_from.isoformat())["changes"]


def test_a_correction_leaves_two_audit_rows_with_the_right_old_and_new(world):
    put_price(world, 400_000, valid_from=day(-10), note="первая")
    put_price(world, 450_000, valid_from=day(-10), note="исправили опечатку")          # the same day: a correction
    put_price(world, 600_000, valid_from=day(5), note="с нового сезона")               # a new step: old = the price in force that day
    first, second, third = audit_rows(world)
    assert [(r.action, r.snapshot["valid_from"], r.snapshot["old_amount"], r.snapshot["new_amount"], r.snapshot["note"],
             r.snapshot["corrected"]) for r in (first, second, third)] == [
        ("group_price_set", day(-10).isoformat(), None, 400_000, "первая", False),
        ("group_price_set", day(-10).isoformat(), 400_000, 450_000, "исправили опечатку", True),
        ("group_price_set", day(5).isoformat(), 450_000, 600_000, "с нового сезона", False),
    ]
    assert second.snapshot["old_note"] == "первая" and first.snapshot["old_note"] is None
    # the whole snapshot, the columns and the author
    assert set(first.snapshot) == {"actor_id", "group_id", "valid_from", "old_amount", "old_note", "new_amount", "note",
                                   "corrected", "impact"}
    assert first.snapshot["actor_id"] == str(world.owner.id) and first.snapshot["group_id"] == str(world.group.id)
    assert first.snapshot["impact"] == {"students_following": 3, "students_with_override": 0, "stale_payroll_sheets": 0}
    for r in (first, second, third):
        assert (r.actor_id, r.actor_role, r.org_id, r.group_id, r.assigned_teacher_id) == (
            world.owner.id, "owner", world.org.id, world.group.id, world.teacher.id)
    # the price rows themselves
    assert group_rows(world) == [(day(-10), 450_000, "исправили опечатку"), (day(5), 600_000, "с нового сезона")]
    # the trail on the API: newest first, only the rows of that valid_from, the old fields of the history unchanged
    got = get_prices(world)
    changes = trail(got, day(-10))
    assert [(c["old_amount"], c["new_amount"], c["note"], c["actor_name"]) for c in changes] == [
        (400_000, 450_000, "исправили опечатку", "Иван Owner"), (None, 400_000, "первая", "Иван Owner")]
    assert [c["at"] for c in changes] == [second.created_at.isoformat(), first.created_at.isoformat()]
    assert set(changes[0]) == {"at", "actor_name", "old_amount", "new_amount", "note"}
    assert [(c["old_amount"], c["new_amount"]) for c in trail(got, day(5))] == [(450_000, 600_000)]
    assert set(got["history"][0]) == {"id", "amount", "valid_from", "note", "created_by", "created_by_name", "created_at",
                                      "is_current", "is_future", "changes"}
    assert put_price(world, 450_000, valid_from=day(-10), note="исправили опечатку")["history"] == got["history"]   # the answer of a PUT carries it too
    # a price written before the trail (the migration, a fixture) has an empty one
    give_group_price(world.db, world.group, 100_000, valid_from=EPOCH)
    assert trail(get_prices(world), EPOCH) == []


def test_a_correction_that_changes_only_the_note_is_audited_with_the_same_amount(world):
    put_price(world, 450_000, valid_from=day(-3), note="сентябрь")
    put_price(world, 450_000, valid_from=day(-3), note="сентябрь, по договору")
    old, new = audit_rows(world)[1].snapshot["old_amount"], audit_rows(world)[1].snapshot["new_amount"]
    assert (old, new) == (450_000, 450_000) and audit_rows(world)[1].snapshot["old_note"] == "сентябрь"


def test_a_double_click_leaves_one_audit_row_and_changes_nothing(world):
    first = put_price(world, 450_000, valid_from=day(-3), note="сентябрь")
    stored = stamp_of_group_price(world)
    again = put_price(world, 450_000, valid_from=day(-3), note="сентябрь")
    assert len(audit_rows(world)) == 1
    assert again["history"] == first["history"] and again["current"] == first["current"]
    assert stamp_of_group_price(world) == stored                                                          # not even the stamp moved
    # the same request from another admin with the money right is the same request: nothing moves, no row
    allow_admin(world)
    put_price(world, 450_000, valid_from=day(-3), note="сентябрь", client=make_client(world, user=world.admin))
    assert len(audit_rows(world)) == 1
    assert stamp_of_group_price(world) == stored
    # an empty note and no note are the same note
    put_price(world, 500_000, valid_from=day(-2))
    put_price(world, 500_000, valid_from=day(-2), note="  ")
    assert len(audit_rows(world)) == 2
    # a different amount, note or day IS a write
    put_price(world, 451_000, valid_from=day(-3), note="сентябрь")
    put_price(world, 451_000, valid_from=day(-3), note="сентябрь+")
    put_price(world, 451_000, valid_from=day(-1), note="сентябрь+")
    assert len(audit_rows(world)) == 5
    # the same for an individual price
    s0 = world.students[0]
    put_override(world, s0, amount=300_000, reason="скидка", valid_from=day(-3))
    stored = stamp_of_override(world, s0)
    put_override(world, s0, amount=300_000, reason="скидка", valid_from=day(-3))
    put_override(world, s0, amount=300_000, reason=" скидка ", valid_from=day(-3), client=make_client(world, user=world.admin))
    assert len(audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)) == 1
    assert stamp_of_override(world, s0) == stored
    put_override(world, s0, amount=300_000, reason="скидка за двоих", valid_from=day(-3))
    put_override(world, s0, amount=310_000, reason="скидка за двоих", valid_from=day(-3))
    assert len(audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)) == 3
    # "the group price again" twice is also one write
    put_override(world, s0, amount=None, reason=None, valid_from=day(-1))
    put_override(world, s0, amount=None, reason=None, valid_from=day(-1))
    assert len(audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)) == 4


def test_an_individual_price_is_audited_with_its_old_effective_price_and_source(world):
    s0, s1 = world.students[0], world.students[1]
    assert put_override(world, s1, amount=100_000, reason="без цены группы", valid_from=day(-9))["price"]["amount"] == 100_000
    only = audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)[0].snapshot
    assert (only["old_amount"], only["old_source"], only["new_amount"]) == (None, "none", 100_000)    # nothing was there
    put_price(world, 500_000, valid_from=day(-30))
    put_override(world, s0, amount=300_000, reason="скидка", valid_from=day(-5))
    put_override(world, s0, amount=250_000, reason="скидка больше", valid_from=day(-5))              # a correction of that day
    rows = [r for r in audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE) if r.snapshot["membership_id"] == str(mid(world, s0))]
    assert [(r.snapshot["old_amount"], r.snapshot["old_source"], r.snapshot["new_amount"], r.snapshot["reason"],
             r.snapshot["old_reason"], r.snapshot["corrected"]) for r in rows] == [
        (500_000, "group", 300_000, "скидка", None, False),                   # the price of the group was in force
        (300_000, "override", 250_000, "скидка больше", "скидка", True),      # the row of that day is what is replaced
    ]
    snap = rows[0].snapshot
    assert set(snap) == {"actor_id", "group_id", "membership_id", "student_id", "valid_from", "old_amount", "old_source",
                         "old_reason", "new_amount", "reason", "corrected"}
    assert (snap["actor_id"], snap["group_id"], snap["student_id"], snap["valid_from"]) == (
        str(world.owner.id), str(world.group.id), str(s0.id), day(-5).isoformat())
    assert (rows[0].actor_id, rows[0].actor_role, rows[0].org_id, rows[0].assigned_teacher_id) == (
        world.owner.id, "owner", world.org.id, world.teacher.id)
    got = get_override(world, s0)
    changes = trail(got, day(-5))
    assert [(c["old_amount"], c["old_source"], c["new_amount"], c["reason"], c["actor_name"]) for c in changes] == [
        (300_000, "override", 250_000, "скидка больше", "Иван Owner"), (500_000, "group", 300_000, "скидка", "Иван Owner")]
    assert set(changes[0]) == {"at", "actor_name", "old_amount", "old_source", "new_amount", "reason"}
    assert get_override(world, s1)["history"][0]["changes"][0]["old_source"] == "none"            # another student: his own trail only
    assert len(trail(got, day(-5))) == 2 and all(len(h["changes"]) == 2 for h in got["history"])


def test_the_way_back_to_the_group_price_is_audited(world):
    s0 = world.students[0]
    put_price(world, 500_000, valid_from=day(-30))
    put_override(world, s0, amount=300_000, reason="скидка", valid_from=day(-5))
    out = put_override(world, s0, amount=None, reason=None, valid_from=day())                # «вернуть цену группы»
    assert out["price"]["source"] == "group" and out["price"]["amount"] == 500_000
    back = audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)[-1]
    assert back.action == "membership_price_set" and back.actor_id == world.owner.id
    assert (back.snapshot["old_amount"], back.snapshot["old_source"], back.snapshot["new_amount"], back.snapshot["reason"]) == (
        300_000, "override", None, None)
    assert trail(out, day()) == [{"at": back.created_at.isoformat(), "actor_name": "Иван Owner", "old_amount": 300_000,
                                  "old_source": "override", "new_amount": None, "reason": None}]
    # the way back with a reason; then a sum on the very day of the way back: the old price is the group's again
    put_override(world, s0, amount=None, reason="вернули по просьбе родителей", valid_from=day())
    again = put_override(world, s0, amount=280_000, reason="снова скидка", valid_from=day())
    last, before = trail(again, day())[:2]
    assert (last["old_amount"], last["old_source"], last["new_amount"]) == (500_000, "group", 280_000)
    assert (before["old_amount"], before["old_source"], before["new_amount"], before["reason"]) == (
        500_000, "group", None, "вернули по просьбе родителей")                        # the row being corrected had no amount: the group price was in force
    # a group price of the day is audited the same way when its row is the first one: no price before -> old null
    fresh = make_group(world.db, world.org, world.teacher2, "Новая")
    world.db.commit()
    put_price(world, 100_000, group=fresh, valid_from=day(-1))
    (row,) = audit_rows(world, group=fresh)
    assert (row.snapshot["old_amount"], row.snapshot["new_amount"], row.snapshot["impact"]["students_following"]) == (None, 100_000, 0)


def test_adding_a_student_with_an_individual_price_is_audited_too(world):
    other = make_group(world.db, world.org, world.teacher2, "Другая")
    world.db.commit()
    put_price(world, 500_000, group=other, valid_from=day(-30))
    student = world.students[0]
    world.svc.add_student_to_group(str(world.org.id), str(other.id), str(student.id), mode="transfer",
                                   price_override=SimpleNamespace(amount=450_000, reason="скидка"), actor_id=str(world.admin.id))
    (row,) = audit_rows(world, group=other, action=pricing.ACTION_MEMBERSHIP_PRICE)
    assert (row.actor_id, row.actor_role, row.snapshot["old_amount"], row.snapshot["old_source"], row.snapshot["new_amount"],
            row.snapshot["student_id"]) == (world.admin.id, "admin", 500_000, "group", 450_000, str(student.id))
    # the student is already there (his only group): the same exception again is nothing, another sum corrects that day
    world.svc.add_student_to_group(str(world.org.id), str(other.id), str(student.id), mode="transfer",
                                   price_override=SimpleNamespace(amount=450_000, reason="скидка"), actor_id=str(world.admin.id))
    assert len(audit_rows(world, group=other, action=pricing.ACTION_MEMBERSHIP_PRICE)) == 1
    world.svc.add_student_to_group(str(world.org.id), str(other.id), str(student.id), mode="transfer",
                                   price_override=SimpleNamespace(amount=400_000, reason="больше скидка"), actor_id=str(world.admin.id))
    assert [r.snapshot["new_amount"] for r in audit_rows(world, group=other, action=pricing.ACTION_MEMBERSHIP_PRICE)] == [
        450_000, 400_000]
    # a membership write nobody can attribute is refused: the trail has no row without an actor
    with pytest.raises(ValueError):
        pricing.write_override(world.db, mid(world, student, other), None, amount=1, valid_from=day(), reason="без автора")
    world.db.rollback()


def test_the_price_and_its_audit_row_commit_or_roll_back_together(world, engine, monkeypatch):
    from sqlalchemy import event

    class Boom(RuntimeError):
        pass

    def after_audit_insert(mapper, connection, target):
        raise Boom("the audit row was inserted, then something failed")

    def fail_commit():
        raise Boom("the commit failed after both rows were written")

    owner = money_owner(world)
    s0 = world.students[0]
    group_body = {"amount": 450_000, "valid_from": day(-3).isoformat(), "note": "n"}
    override_url = f"{API}/org/memberships/{mid(world, s0)}/price-override"
    override_body = {"amount": 300_000, "reason": "скидка", "valid_from": day(-3).isoformat()}
    group_url = f"{API}/org/groups/{world.group.id}/price"

    def put_group():
        return owner.put(group_url, json=group_body)

    def put_member():
        return owner.put(override_url, json=override_body)

    assert committed_state(engine, world) == (0, 0, 0)
    for request in (put_group, put_member):
        # a failure AFTER the audit row was inserted in the transaction (the price row went in before it) ...
        event.listen(GroupAudit, "after_insert", after_audit_insert)
        try:
            with pytest.raises(Boom):
                request()
        finally:
            event.remove(GroupAudit, "after_insert", after_audit_insert)
        assert committed_state(engine, world) == (0, 0, 0)
        # ... and a failure of the commit itself, both rows written
        monkeypatch.setattr(world.db, "commit", fail_commit)
        try:
            with pytest.raises(Boom):
                request()
        finally:
            monkeypatch.undo()
        assert committed_state(engine, world) == (0, 0, 0)
    # the control: without the failure both are there, together
    assert put_group().status_code == 200 and committed_state(engine, world) == (1, 0, 1)
    assert put_member().status_code == 200 and committed_state(engine, world) == (1, 1, 2)
    # a failed CORRECTION keeps the old amount and adds no row: the update and the audit row go together too
    group_body["amount"], override_body["amount"] = 999_000, 888_000
    for request, kept, table_sql, key in (
        (put_group, 450_000, "SELECT amount FROM group_prices WHERE group_id = :k", world.group.id),
        (put_member, 300_000, "SELECT amount FROM membership_price_overrides WHERE membership_id = :k", mid(world, s0)),
    ):
        before = committed_state(engine, world)
        event.listen(GroupAudit, "after_insert", after_audit_insert)
        try:
            with pytest.raises(Boom):
                request()
        finally:
            event.remove(GroupAudit, "after_insert", after_audit_insert)
        assert committed_state(engine, world) == before
        assert committed(engine, table_sql, k=key) == kept
        assert request().status_code == 200                                     # the session is clean: the retry is a correction
        assert committed_state(engine, world) == (before[0], before[1], before[2] + 1)
    assert committed(engine, "SELECT amount FROM group_prices WHERE group_id = :g", g=world.group.id) == 999_000
    assert [(r.snapshot["old_amount"], r.snapshot["new_amount"]) for r in audit_rows(world, pricing.ACTION_GROUP_PRICE)] == [
        (None, 450_000), (450_000, 999_000)]
    assert [(r.snapshot["old_amount"], r.snapshot["new_amount"]) for r in audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)] == [
        (450_000, 300_000), (300_000, 888_000)]


def blocked_probe(engine, sql, **params):
    """``True`` if the row lock asked for by ``sql`` (… FOR NO KEY UPDATE NOWAIT) is held by somebody else."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    with Session(engine) as probe:
        try:
            probe.execute(text(sql), params)
            return False
        except OperationalError as exc:
            assert exc.orig.pgcode == "55P03", exc
            return True
        finally:
            probe.rollback()


def test_a_price_write_holds_the_student_then_waits_for_the_group_before_it_takes_the_membership(world, engine):
    """The lock order of an individual price is student -> group -> membership (module docstring of ``pricing``).  A holder
    of the group that then asks for the membership - ``remove_student_from_group`` - used to close a cycle with the audit
    row (the foreign key to the group); now the write waits for the group holding the student only, the membership is free."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    student, membership_id = world.students[0], mid(world)
    holder = Session(engine)
    holder.execute(text("SELECT id FROM groups WHERE id = :g FOR UPDATE"), {"g": world.group.id})      # the group first ...
    thread, outcome = run_in_thread(engine, lambda session: pricing.PricingService(session).set_override(
        str(world.org.id), membership_id, str(world.owner.id), amount=1_000, valid_from=day(-3), reason="порядок"))
    wait_until_blocked(engine)                                                      # the write waits for the group ...
    assert blocked_probe(engine, "SELECT id FROM students WHERE id = :s FOR NO KEY UPDATE NOWAIT", s=student.id)    # ... holding the student
    assert not blocked_probe(engine, "SELECT id FROM student_groups WHERE id = :m FOR NO KEY UPDATE NOWAIT", m=membership_id)   # ... not the membership
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR NO KEY UPDATE NOWAIT"), {"m": membership_id})   # ... then the membership: at once
    holder.commit()
    holder.close()
    out = finish(thread, outcome)
    assert "error" not in out, out.get("error")
    world.db.expire_all()
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=membership_id).count() == 1
    assert len(audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)) == 1


def test_a_price_write_waiting_for_an_archiving_sees_the_archive_and_writes_nothing(world, engine):
    """The status of the student is read after the locks: an archiving in flight (it holds the student row) is waited for
    and seen - the clean 409, no override, no audit row - never a write on the stale 'active'."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    student, membership_id = world.students[0], mid(world)
    holder = Session(engine)
    holder.execute(text("UPDATE students SET status = 'archived', archived_at = now() WHERE id = :s"), {"s": student.id})
    thread, outcome = run_in_thread(engine, lambda session: pricing.PricingService(session).set_override(
        str(world.org.id), membership_id, str(world.owner.id), amount=1_000, valid_from=day(-3), reason="архив"))
    wait_until_blocked(engine)                                                      # it waits for the student row
    holder.commit()
    holder.close()
    out = finish(thread, outcome)
    assert isinstance(out.get("error"), pricing.PriceConflict) and out["error"].message == "Ученик в архиве — сначала верните его"
    world.db.expire_all()
    assert world.db.query(MembershipPriceOverride).filter_by(membership_id=membership_id).count() == 0
    assert audit_rows(world) == []


def race_price_write_with(world, engine, monkeypatch, caplog, rounds, competitor, reset=None):
    """``rounds`` times: a price write on a membership and ``competitor(session)`` (a writer of the SAME student) right
    behind it.  The audit insert is slowed to 40 ms and the competitor starts only once the price write holds the student
    row, so it arrives while the price write is inside its transaction - the window of the old cycle is wide open.  Every round must end clean on both sides: PostgreSQL
    aborts exactly one participant of a deadlock, so a missing error is a missing deadlock."""
    import logging
    import time

    original = pricing._add_audit

    def slow_audit(*args, **kwargs):
        time.sleep(0.04)
        return original(*args, **kwargs)

    monkeypatch.setattr(pricing, "_add_audit", slow_audit)
    membership_id = mid(world)
    caplog.set_level(logging.WARNING, logger="src.auth.pricing")
    for n in range(rounds):
        price_thread, price_out = run_in_thread(engine, lambda session, n=n: pricing.PricingService(session).set_override(
            str(world.org.id), membership_id, str(world.owner.id), amount=100_000 + n, valid_from=day(-3), reason=f"гонка {n}"))
        deadline = time.time() + 10
        while not blocked_probe(engine, "SELECT id FROM students WHERE id = :s FOR NO KEY UPDATE NOWAIT", s=world.students[0].id):
            assert time.time() < deadline, "the price write never took the student row"
            time.sleep(0.001)                                                       # the price write is first, inside its locks
        other_thread, other_out = run_in_thread(engine, competitor)
        outcomes = (finish(price_thread, price_out), finish(other_thread, other_out))
        assert all("error" not in o for o in outcomes), (n, [o.get("error") for o in outcomes])
        world.db.expire_all()
        if reset:
            reset(n)
    monkeypatch.undo()
    assert not [r for r in caplog.records if "deadlock" in r.getMessage()]           # the safety net of _guard never had to act
    world.db.expire_all()
    amounts = [r.snapshot["new_amount"] for r in audit_rows(world, pricing.ACTION_MEMBERSHIP_PRICE)
               if r.snapshot["reason"] and r.snapshot["reason"].startswith("гонка")]
    assert amounts == [100_000 + n for n in range(rounds)]                          # every price write is there, once, in order


def test_remove_student_and_a_price_write_on_the_same_membership_never_deadlock(world, engine, monkeypatch, caplog):
    student = world.students[0]

    def remove(session):
        return AuthService(session).remove_student_from_group(str(world.org.id), str(world.group.id), str(student.id))

    def back(n):                                                                    # the student returns to the group for the next round
        world.svc.add_student_to_group(str(world.org.id), str(world.group.id), str(student.id), mode="transfer")

    race_price_write_with(world, engine, monkeypatch, caplog, 25, remove, back)


def test_archiving_and_a_price_write_on_the_same_membership_never_deadlock(world, engine, monkeypatch, caplog):
    from src.auth.student_archive import StudentArchiveService

    student = world.students[0]

    def archive(session):
        return StudentArchiveService(session).archive(str(world.org.id), str(student.id), actor_id=str(world.owner.id),
                                                      actor_role="owner")

    def restore(n):
        StudentArchiveService(world.db).restore(str(world.org.id), str(student.id), actor_id=str(world.owner.id),
                                                actor_role="owner", group_ids=[world.group.id])

    race_price_write_with(world, engine, monkeypatch, caplog, 10, archive, restore)


def test_enrolment_with_an_individual_price_and_a_price_write_on_the_same_membership_never_deadlock(world, engine, monkeypatch, caplog):
    student = world.students[0]

    def enrol(session):                                                             # the student is in the group: the "nothing moves" path writes the exception
        return AuthService(session).add_student_to_group(
            str(world.org.id), str(world.group.id), str(student.id), mode="transfer",
            price_override=SimpleNamespace(amount=222_000, reason="при зачислении"), actor_id=str(world.admin.id))

    race_price_write_with(world, engine, monkeypatch, caplog, 10, enrol)


def test_the_audit_trail_is_for_the_money_roles_only(world):
    s0 = world.students[0]
    put_price(world, 450_000, valid_from=day(-3), note="секретная заметка")
    put_price(world, 460_000, valid_from=day(-3), note="секретная заметка")
    put_override(world, s0, amount=311_111, reason="секретная причина", valid_from=day(-3))
    prices, override = f"{API}/org/groups/{world.group.id}/prices", f"{API}/org/memberships/{mid(world)}/price-override"
    before = len(audit_rows(world))
    # a teacher and an admin without the money right: 403 on the trail and on every write that would make a row
    for user in (world.teacher, world.admin):
        client = make_client(world, user=user)
        assert client.get(prices).status_code == 403 and client.get(override).status_code == 403
        assert client.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 1}).status_code == 403
        assert client.put(override, json={"amount": 1, "reason": "проверка"}).status_code == 403
        assert client.put(override, json={"amount": None}).status_code == 403
        for url in (f"{API}/org/groups/{world.group.id}", f"{API}/org/students/{world.students[0].id}/profile",
                    f"{API}/org/students/{world.students[0].id}/memberships", f"{API}/org/groups"):
            r = client.get(url)
            for secret in ("changes", "group_price_set", "membership_price_set", "секретная", "311111", "460000"):
                assert secret not in r.text, (user.role, url, secret)
    assert len(audit_rows(world)) == before
    # the admin with the owner's permission sees the trail and writes into it with his own name and role
    allow_admin(world)
    admin = make_client(world, user=world.admin)
    assert [c["new_amount"] for c in trail(get_prices(world, client=admin), day(-3))] == [460_000, 450_000]
    assert trail(get_override(world, s0, client=admin), day(-3))[0]["new_amount"] == 311_111
    put_price(world, 470_000, valid_from=day(-3), client=admin)
    last = audit_rows(world, pricing.ACTION_GROUP_PRICE)[-1]
    assert (last.actor_id, last.actor_role, last.snapshot["old_amount"]) == (world.admin.id, "admin", 460_000)
    assert trail(get_prices(world), day(-3))[0]["actor_name"] == "Иван Admin"
    # the trail of another centre is not reachable: 404, not a leak
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="owner", role="owner")
    world.db.query(__import__("src.models.auth", fromlist=["OrganizationMember"]).OrganizationMember).filter_by(
        user_id=world.foreign_admin.id).update({"org_role": "owner"})
    world.db.commit()
    assert get_prices(world, client=foreign, expect=404) and get_override(world, client=foreign, expect=404)


def test_a_dry_run_and_a_refused_write_leave_no_audit_row(world):
    put_price(world, 450_000, dry=True)
    put_price(world, -5, expect=422)
    put_override(world, amount=1, reason="  ", expect=422)
    world.db.query(Student).filter_by(id=world.students[0].id).update({"status": "archived"})
    world.db.commit()
    put_override(world, world.students[0], amount=1, reason="архив", expect=409)
    assert audit_rows(world) == []


# ============================================================================================
# the dry-run script
# ============================================================================================


def run_dry_run(*extra):
    script = Path(__file__).resolve().parents[1] / "scripts" / "group_price_dry_run.py"
    done = subprocess.run([sys.executable, str(script), *extra], capture_output=True, text=True, timeout=120,
                          env={"DATABASE_URL": DB_URL, "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_dry_run_script_prints_the_plan_and_writes_nothing(world):
    name = f"Сухой прогон {uuid4().hex[:8]}"
    world.org.name = name
    world.db.commit()
    g2 = make_group(world.db, world.org, world.teacher2, "Вторая")
    g3 = make_group(world.db, world.org, world.teacher2, "Третья")
    extra = [make_student(world.db, world.org, world.group, f"Доп{i}", "Тестов", f"N13-9{i}") for i in range(2)]
    pupil = make_student(world.db, world.org, g2, "Один", "Тестов", "N13-950")
    cold = make_student(world.db, world.org, g3, "Холод", "Тестов", "N13-951")
    world.db.commit()
    fees = {world.students[0]: 500_000, world.students[1]: 500_000, world.students[2]: 450_000, extra[0]: None, extra[1]: None,
            pupil: 300_000, cold: None}
    for student, fee in fees.items():
        world.db.query(StudentGroup).filter_by(student_id=student.id).update({"monthly_fee": fee})
    world.db.commit()

    def counts():
        world.db.expire_all()
        return (world.db.query(GroupPrice).count(), world.db.query(MembershipPriceOverride).count(),
                sorted((str(m.id), m.monthly_fee) for m in world.db.query(StudentGroup)))

    before = counts()
    data = json.loads(run_dry_run("--json"))
    mine = {g["group"]: g for g in data["groups"] if g["org"] == name}
    assert set(mine) == {"Группа", "Вторая", "Третья"}
    first = mine["Группа"]
    assert (first["price"], first["votes"], first["tie"], first["memberships"]) == (500_000, 2, False, 5)
    assert (first["follow_active"], first["overrides"], first["null_fee_start_following_active"]) == (4, 1, 2)
    assert (mine["Вторая"]["price"], mine["Вторая"]["follow_active"], mine["Вторая"]["overrides"]) == (300_000, 1, 0)
    third = mine["Третья"]
    assert third["price"] is None and third["follow_active"] == 0 and third["overrides"] == 0 and third["null_fee_start_following_active"] == 0
    text = run_dry_run("--verbose")
    assert name in text and "500 000" in text and "450 000 × 1" in text and "Итого" in text and "НАЧНУТ БРАТЬ ЦЕНУ" in text
    assert "Каримова" not in text and "Алия" not in text                              # no student names
    assert counts() == before                                                         # it wrote nothing
    assert "migration_already_applied" in data


def test_the_dry_run_script_connection_is_read_only(world):
    script = Path(__file__).resolve().parents[1] / "scripts" / "group_price_dry_run.py"
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location("dry_run_script", script)
    module = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    spec.loader.exec_module(module)
    plans, target, applied = module.collect(DB_URL)
    assert target == "a2_auth" and applied is True                                     # the tables exist in the test database
    assert module.main(["--database-url", ""]) == 2                                    # no URL: refuses
