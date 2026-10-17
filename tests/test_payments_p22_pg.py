"""N13 P2.2: one payment day, the amount hint (quote), receipts, cheques (PostgreSQL)."""
import threading
from datetime import date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.auth import payment_status as ps
from src.auth.payments_schemas import PaymentCreate
from src.auth.payments_service import RECEIPT_ATTEMPTS, PaymentConflict, PaymentService
from src.models.auth import (
    GroupAttendance, GroupScheduleOneOffLesson, StudentGroup, StudentPayment, StudentPaymentFile,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_client, make_group, make_member, make_user, needs_pg, pg_session, world,
)
from tests.test_payment_carry_over_pg import transfer, two  # noqa: F401  (two = fixture)
from tests.test_payments_service_pg import API, day, mid, owner, pay, row_of, set_settings

pytestmark = needs_pg

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"%" * 64
D = date

MARCH = D(2026, 3, 1)  # Mon/Wed/Fri lessons in March 2026: 2 4 6 9 11 13 16 18 20 23 25 27 30 (13)


# ---- helpers ----------------------------------------------------------------------


def as_admin(w, flag=True):
    if flag:
        set_settings(w, admin_can_manage=True)
    return make_client(w, user=w.admin)


def teacher_client(w):
    return make_client(w, user=w.teacher)


def quote(w, membership_id=None, client=None, expect=200, **params):
    for key, value in list(params.items()):
        if isinstance(value, date):
            params[key] = value.isoformat()
    r = (client or owner(w)).get(
        f"{API}/org/memberships/{membership_id or mid(w)}/payments/quote", params=params)
    assert r.status_code == expect, r.text
    return r.json()


def stored_payment(w, payment_id):
    w.db.expire_all()
    return w.db.get(StudentPayment, payment_id)


def short(day_):
    """How the explanation writes a date: ``15.03`` in the current year, ``15.03.2025`` in another."""
    return day_.strftime("%d.%m") if day_.year == business_today().year else day_.strftime("%d.%m.%Y")


def nfiles(w):
    return w.db.query(StudentPaymentFile).filter_by(org_id=w.org.id).count()


def setup_mwf(w, fee=450_000, billing_day=1, joined=None, anchor="fixed_day"):
    """Group lessons on Monday/Wednesday/Friday; student 0 pays ``fee``; the centre has one payment day."""
    membership_id = mid(w)                       # (expires the session: do it before any change)
    w.group.schedule = [
        {"id": name, "dayOfWeek": dow, "startTime": "10:00", "endTime": "11:00", "format": "offline"}
        for name, dow in (("mon", 1), ("wed", 3), ("fri", 5))
    ]
    membership = w.db.get(StudentGroup, membership_id)
    membership.monthly_fee = fee
    if joined is not None:
        membership.joined_at = joined
        for stay in membership.periods:              # the last stay mirrors the row (src/auth/membership_periods.py)
            stay.joined_at = joined
    w.db.commit()
    set_settings(w, period_mode="calendar", billing_anchor=anchor, billing_day=billing_day)
    return membership


def cancel_lesson(w, lesson_date, lesson_id):
    w.db.add(GroupAttendance(
        org_id=w.org.id, group_id=w.group.id, lesson_date=lesson_date, lesson_id=lesson_id,
        status="cancelled", records={}, roster_snapshot=[], cancellation_reason="Праздник"))
    w.db.commit()


# ---- settings ---------------------------------------------------------------------


def test_billing_settings_are_stored_and_patched(world):
    body = set_settings(world, billing_anchor="fixed_day", billing_day=10)
    assert (body["billing_anchor"], body["billing_day"], body["period_mode"]) == ("fixed_day", 10, "calendar")
    body = set_settings(world, expiring_days=4)                       # untouched fields stay
    assert (body["billing_anchor"], body["billing_day"]) == ("fixed_day", 10)
    for bad in ({"billing_day": 0}, {"billing_day": 29}, {"billing_anchor": "weekly"}):
        assert owner(world).put(f"{API}/org/settings/payments", json=bad).status_code == 422
    # a stored garbage value falls back to the defaults, the page still opens
    world.org.settings = {"payments": {"billing_day": 31, "billing_anchor": "fixed_day"}}
    world.db.commit()
    got = owner(world).get(f"{API}/org/settings/payments").json()
    assert (got["billing_anchor"], got["billing_day"]) == ("payment_date", 1)


# ---- quote: dates are the dates payment creation uses -------------------------------


def test_quote_first_payment_of_a_student_who_came_mid_month(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 17))
    assert (q["mode"], q["billing_anchor"], q["billing_day"]) == ("calendar", "fixed_day", 1)
    assert (q["valid_from"], q["valid_to"], q["lessons_count"], q["partial"]) == ("2026-03-15", "2026-03-31", None, True)
    # 16 18 20 23 25 27 30 = 7 of 13  ->  450 000 * 7 / 13 = 242 307 -> 242 000
    assert q["basis"] == "lessons" and q["monthly_fee"] == 450_000 and q["suggested_amount"] == 242_000
    (part,) = q["parts"]
    assert part == {"from": "2026-03-15", "to": "2026-03-31", "period_from": "2026-03-01", "period_to": "2026-03-31",
                    "lessons": 7, "period_lessons": 13, "amount": 242_000}
    assert q["explanation"] == "Неполный месяц: 7 уроков из 13 по расписанию — 242 000 сум из 450 000"
    # the very same dates are what creating the payment without dates saves
    created = pay(world, paid_on=D(2026, 3, 17), amount=q["suggested_amount"])["payment"]
    assert (created["valid_from"], created["valid_to"], created["partial_period"]) == ("2026-03-15", "2026-03-31", True)


def test_quote_uses_the_business_date_of_joining_not_the_utc_date(world):
    # 14.03 20:30 UTC is 15.03 01:30 in Tashkent
    setup_mwf(world, joined=datetime(2026, 3, 14, 20, 30))
    assert quote(world, paid_on=D(2026, 3, 17))["valid_from"] == "2026-03-15"
    # 14.03 18:30 UTC is still 14.03 in Tashkent... 23:30
    world.db.get(StudentGroup, mid(world)).joined_at = datetime(2026, 3, 14, 18, 30)
    world.db.commit()
    assert quote(world, paid_on=D(2026, 3, 17))["valid_from"] == "2026-03-14"


def test_quote_a_student_who_came_before_the_period_pays_the_whole_period(world):
    setup_mwf(world, joined=datetime(2026, 2, 25, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 3))
    assert (q["valid_from"], q["valid_to"], q["partial"], q["basis"]) == ("2026-03-01", "2026-03-31", False, "full")
    assert (q["suggested_amount"], q["explanation"]) == (450_000, "Полный месяц — 450 000 сум")
    assert q["parts"] == [{"from": "2026-03-01", "to": "2026-03-31", "period_from": "2026-03-01",
                           "period_to": "2026-03-31", "lessons": None, "period_lessons": None, "amount": 450_000}]


def test_quote_without_a_joining_date_starts_on_the_payment_date(world):
    setup_mwf(world)
    world.db.get(StudentGroup, mid(world)).joined_at = None
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 17))
    assert (q["valid_from"], q["valid_to"], q["partial"]) == ("2026-03-17", "2026-03-31", True)


def test_a_student_who_rejoins_reuses_the_row_and_starts_on_the_return_day(world, monkeypatch):
    """Remove + add again reactivates the SAME membership and resets joined_at; old payments must not
    stretch the new period back to the start of the month."""
    from tests.test_payment_carry_over_pg import GROUPS, clock

    setup_mwf(world, joined=datetime(2026, 1, 5, 6, 0))
    student = world.students[0]
    old_id = mid(world)
    pay(world, paid_on=D(2026, 2, 1), valid_from=D(2026, 2, 1), valid_to=D(2026, 2, 28))   # paid until 28.02
    with clock(monkeypatch, D(2026, 3, 1), 11):      # he leaves before he returns (a stay never starts before the last one ended)
        assert owner(world).delete(f"{GROUPS}/{world.group.id}/students/{student.id}").status_code == 200
    with clock(monkeypatch, D(2026, 3, 15), 11):
        r = owner(world).post(f"{GROUPS}/{world.group.id}/students/{student.id}")
        assert r.status_code == 200, r.text
    membership = world.db.query(StudentGroup).filter_by(student_id=student.id, group_id=world.group.id).one()
    assert membership.id == old_id and membership.is_active is True                          # the same row
    assert membership.joined_at == datetime(2026, 3, 15, 6, 0)                              # reset to the return moment
    q = quote(world, paid_on=D(2026, 3, 17))
    assert (q["valid_from"], q["valid_to"], q["partial"]) == ("2026-03-15", "2026-03-31", True)
    assert q["basis"] == "lessons" and q["parts"][0]["lessons"] == 7                          # 16 18 20 23 25 27 30
    created = pay(world, paid_on=D(2026, 3, 17), amount=q["suggested_amount"])["payment"]
    assert (created["valid_from"], created["valid_to"], created["partial_period"]) == ("2026-03-15", "2026-03-31", True)
    # next month the (continuing) student gets the whole period again
    assert quote(world, paid_on=D(2026, 4, 2))["valid_from"] == "2026-04-01"


def test_quote_late_payment_is_still_the_next_period_and_prepayment_continues(world):
    setup_mwf(world, joined=datetime(2026, 1, 5, 6, 0))
    pay(world, paid_on=D(2026, 2, 1), valid_from=D(2026, 2, 1), valid_to=D(2026, 2, 28))
    late = quote(world, paid_on=D(2026, 3, 3))                      # two days late
    assert (late["valid_from"], late["valid_to"], late["suggested_amount"]) == ("2026-03-01", "2026-03-31", 450_000)
    pay(world, paid_on=D(2026, 3, 3), valid_from=D(2026, 3, 1), valid_to=D(2026, 3, 31), amount=1)
    ahead = quote(world, paid_on=D(2026, 3, 25))                    # paid ahead
    assert (ahead["valid_from"], ahead["valid_to"], ahead["partial"]) == ("2026-04-01", "2026-04-30", False)
    # a month was skipped: the period of the payment date, nothing invented
    gap = quote(world, paid_on=D(2026, 6, 2))
    assert (gap["valid_from"], gap["valid_to"]) == ("2026-06-01", "2026-06-30")


def test_quote_billing_day_ten_crosses_the_month_and_the_year(world):
    setup_mwf(world, billing_day=10, joined=datetime(2025, 12, 1, 6, 0))
    q = quote(world, paid_on=D(2025, 12, 12))
    assert (q["valid_from"], q["valid_to"], q["partial"]) == ("2025-12-10", "2026-01-09", False)
    q = quote(world, paid_on=D(2026, 1, 5), valid_from=D(2025, 12, 10))                 # a client-chosen start
    assert (q["valid_from"], q["valid_to"]) == ("2025-12-10", "2026-01-09")
    # joined 15.03: partial 15.03 - 09.04 of the period 10.03 - 09.04 (13 lessons, 11 from the 15th)
    world.db.get(StudentGroup, mid(world)).joined_at = datetime(2026, 3, 15, 6, 0)
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 16))
    assert (q["valid_from"], q["valid_to"]) == ("2026-03-15", "2026-04-09")
    assert q["parts"][0]["period_from"] == "2026-03-10" and q["parts"][0]["period_to"] == "2026-04-09"
    assert (q["parts"][0]["lessons"], q["parts"][0]["period_lessons"], q["suggested_amount"]) == (11, 13, 381_000)


def test_quote_billing_day_twenty_eight_and_a_period_that_ends_mid_period(world):
    setup_mwf(world, billing_day=28, joined=datetime(2026, 2, 1, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 30))
    assert (q["valid_from"], q["valid_to"], q["partial"]) == ("2026-03-28", "2026-04-27", False)
    assert q["suggested_amount"] == 450_000
    # leaves on the 10th of the next period: the incomplete tail is prorated by lessons too
    q = quote(world, paid_on=D(2026, 3, 30), valid_to=D(2026, 4, 10))
    assert q["partial"] is True and q["basis"] == "lessons"
    (part,) = q["parts"]
    assert (part["from"], part["to"], part["period_from"], part["period_to"]) == (
        "2026-03-28", "2026-04-10", "2026-03-28", "2026-04-27")
    assert part["lessons"] < part["period_lessons"] and q["suggested_amount"] == part["amount"]
    # 31.01 (the 28th rule, a short February): the next period starts on 28.02
    q = quote(world, paid_on=D(2026, 2, 5), valid_from=D(2026, 1, 31))
    assert (q["valid_from"], q["valid_to"]) == ("2026-01-31", "2026-02-27")


def test_quote_client_dates_are_used_as_they_are(world):
    setup_mwf(world, joined=datetime(2026, 1, 5, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 17), valid_from=D(2026, 3, 10))                  # no valid_to -> end of the period
    assert (q["valid_from"], q["valid_to"], q["partial"]) == ("2026-03-10", "2026-03-31", True)
    q = quote(world, paid_on=D(2026, 3, 17), valid_to=D(2026, 3, 20))                    # no valid_from -> the default one
    assert (q["valid_from"], q["valid_to"]) == ("2026-03-01", "2026-03-20")
    q = quote(world, paid_on=D(2026, 3, 17), valid_from=D(2026, 3, 1), valid_to=D(2026, 4, 30))
    assert (q["partial"], q["basis"], q["suggested_amount"], len(q["parts"])) == (False, "full", 900_000, 2)
    assert q["explanation"] == "Полных месяцев: 2 — 900 000 сум"


def test_quote_treats_empty_optional_parameters_as_not_given(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    r = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote",
                         params={"paid_on": "2026-03-17", "valid_from": "", "valid_to": "", "lessons_count": ""})
    assert r.status_code == 200, r.text
    assert (r.json()["valid_from"], r.json()["valid_to"]) == ("2026-03-15", "2026-03-31")
    r = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote",
                         params={"paid_on": "2026-03-17", "valid_from": "not-a-date"})
    assert r.status_code == 422


def test_quote_range_over_two_periods_prices_each_one(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 17), valid_to=D(2026, 4, 30))
    assert q["valid_from"] == "2026-03-15" and q["partial"] is True and q["basis"] == "lessons"
    assert [p["amount"] for p in q["parts"]] == [242_000, 450_000]
    assert q["suggested_amount"] == 692_000
    assert q["explanation"] == f"{short(D(2026, 3, 15))}–{short(D(2026, 3, 31))}: 7 уроков из 13 — 242 000 сум; " \
                               f"{short(D(2026, 4, 1))}–{short(D(2026, 4, 30))}: полный месяц — 450 000 сум. " \
                               "Итого 692 000 сум"


def test_quote_cancelled_lessons_do_not_count(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    cancel_lesson(world, "2026-03-18", "wed")      # in the covered part
    cancel_lesson(world, "2026-03-04", "wed")      # before it, but in the period
    cancel_lesson(world, "2026-03-20", "other")    # a lesson id that is not on the schedule: nothing to remove
    q = quote(world, paid_on=D(2026, 3, 17))
    part = q["parts"][0]
    assert (part["lessons"], part["period_lessons"]) == (6, 11) and q["suggested_amount"] == 245_000   # 450000*6/11
    assert q["explanation"].startswith("Неполный месяц: 6 уроков из 11 по расписанию")


def test_quote_counts_one_off_lessons_and_ignores_other_groups_cancellations(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    world.db.add(GroupScheduleOneOffLesson(
        id=f"oneoff-{uuid4()}", org_id=world.org.id, group_id=world.group.id, lesson_date="2026-03-28",
        payload={"dayOfWeek": 6, "startTime": "12:00", "endTime": "13:00"}, created_by=world.owner.id))
    other = make_group(world.db, world.org, world.teacher2, "Другая")
    world.db.add(GroupAttendance(
        org_id=world.org.id, group_id=other.id, lesson_date="2026-03-18", lesson_id="wed",
        status="cancelled", records={}, roster_snapshot=[]))
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 17))
    part = q["parts"][0]
    assert (part["lessons"], part["period_lessons"]) == (8, 14) and q["suggested_amount"] == 257_000   # 450000*8/14


def test_quote_a_group_without_a_schedule_is_counted_by_days(world):
    setup_mwf(world, fee=310_000, joined=datetime(2026, 3, 15, 6, 0))
    world.group.schedule = []
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 17))
    assert q["basis"] == "days" and q["suggested_amount"] == 170_000                        # 310000 * 17 / 31
    assert q["parts"][0]["lessons"] is None and q["parts"][0]["period_lessons"] is None
    assert q["explanation"] == ("Неполный месяц: 17 дней из 31 (уроков по расписанию нет, считаем по дням) "
                                "— 170 000 сум из 310 000")


def test_quote_without_a_monthly_fee_suggests_no_amount(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    world.db.get(StudentGroup, mid(world)).monthly_fee = None
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 17))
    assert (q["suggested_amount"], q["basis"], q["monthly_fee"], q["parts"]) == (None, "none", None, [])
    assert q["explanation"] == "Оплата в месяц не задана" and q["valid_from"] == "2026-03-15"   # the dates still come


def test_quote_from_the_payment_date_standard_and_custom_periods(world):
    setup_mwf(world, anchor="payment_date")
    q = quote(world, paid_on=D(2026, 3, 5))
    assert (q["valid_from"], q["valid_to"], q["partial"], q["basis"], q["suggested_amount"]) == (
        "2026-03-05", "2026-04-04", False, "full", 450_000)
    assert q["explanation"] == "Полный месяц — 450 000 сум" and q["parts"] == []
    q = quote(world, paid_on=D(2026, 3, 5), valid_to=D(2026, 3, 20))
    assert (q["basis"], q["suggested_amount"]) == ("custom", None)
    assert q["explanation"] == "Нестандартный период — укажите сумму"
    q = quote(world, paid_on=D(2026, 3, 31))                        # 31.03 -> 30.04
    assert (q["valid_to"], q["suggested_amount"]) == ("2026-04-30", 450_000)
    world.db.get(StudentGroup, mid(world)).monthly_fee = None
    world.db.commit()
    assert quote(world, paid_on=D(2026, 3, 5))["basis"] == "none"


def test_quote_lessons_package_full_and_non_standard_size(world):
    setup_mwf(world, fee=600_000)
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    q = quote(world, paid_on=D(2026, 3, 5))
    assert (q["mode"], q["valid_to"], q["lessons_count"], q["partial"], q["basis"], q["suggested_amount"]) == (
        "lessons", None, 12, False, "package", 600_000)
    assert q["explanation"] == "Пакет из 12 уроков — 600 000 сум"
    q = quote(world, paid_on=D(2026, 3, 5), lessons_count=5)
    assert (q["lessons_count"], q["suggested_amount"]) == (5, 250_000)
    assert q["explanation"] == "Пакет из 5 уроков вместо 12: 250 000 сум из 600 000"
    q = quote(world, paid_on=D(2026, 3, 5), lessons_count=7)                 # 350 000 exactly
    assert q["suggested_amount"] == 350_000
    set_settings(world, lessons_per_payment=7)
    q = quote(world, paid_on=D(2026, 3, 5), lessons_count=3)                 # 600000 * 3 / 7 = 257 142 -> 257 000
    assert q["suggested_amount"] == 257_000
    # an explicit lessons count while the centre is on calendar periods is still a package
    set_settings(world, period_mode="calendar")
    assert quote(world, paid_on=D(2026, 3, 5), lessons_count=7)["basis"] == "package"


def test_quote_given_fee_replaces_the_saved_fee_for_the_calculation_only(world):
    setup_mwf(world, fee=450_000, joined=datetime(2026, 3, 15, 6, 0))
    saved = quote(world, paid_on=D(2026, 3, 17))
    assert (saved["monthly_fee"], saved["suggested_amount"]) == (450_000, 242_000)
    q = quote(world, paid_on=D(2026, 3, 17), monthly_fee=600_000)
    assert q["monthly_fee"] == 600_000                                     # the fee that was used
    assert q["suggested_amount"] == 323_000 and q["parts"][0]["amount"] == 323_000      # 600000 * 7 / 13 = 323 076
    assert q["explanation"] == "Неполный месяц: 7 уроков из 13 по расписанию — 323 000 сум из 600 000"
    assert world.db.get(StudentGroup, mid(world)).monthly_fee == 450_000   # nothing was written
    assert quote(world, paid_on=D(2026, 3, 17))["monthly_fee"] == 450_000
    assert quote(world, paid_on=D(2026, 3, 17), monthly_fee="")["monthly_fee"] == 450_000   # blank = not given
    # a given fee of 0 is a fee (free), not "not given"
    q = quote(world, paid_on=D(2026, 3, 17), monthly_fee=0)
    assert (q["monthly_fee"], q["suggested_amount"]) == (0, 0)
    assert q["explanation"] == "Оплата в месяц равна 0 — занятия бесплатные"
    # a whole period and the other modes use it too
    assert quote(world, paid_on=D(2026, 4, 2), monthly_fee=500_000)["suggested_amount"] == 500_000
    set_settings(world, billing_anchor="payment_date")
    assert quote(world, paid_on=D(2026, 3, 5), monthly_fee=520_000)["suggested_amount"] == 520_000
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    q = quote(world, paid_on=D(2026, 3, 5), lessons_count=6, monthly_fee=600_000)
    assert (q["monthly_fee"], q["suggested_amount"], q["basis"]) == (600_000, 300_000, "package")


def test_quote_given_fee_when_nothing_is_saved(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    world.db.get(StudentGroup, mid(world)).monthly_fee = None
    world.db.commit()
    none = quote(world, paid_on=D(2026, 3, 17))
    assert (none["monthly_fee"], none["suggested_amount"], none["basis"]) == (None, None, "none")
    q = quote(world, paid_on=D(2026, 3, 17), monthly_fee=450_000)
    assert (q["monthly_fee"], q["suggested_amount"], q["basis"]) == (450_000, 242_000, "lessons")
    assert world.db.get(StudentGroup, mid(world)).monthly_fee is None      # still not saved


def test_quote_given_fee_is_validated(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    for bad in (-1, 10_000_000_001, "abc", "1.5"):
        r = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote",
                             params={"paid_on": "2026-03-17", "monthly_fee": bad})
        assert r.status_code == 422, (bad, r.text)
    q = quote(world, paid_on=D(2026, 3, 17), monthly_fee=10_000_000_000)
    assert q["monthly_fee"] == 10_000_000_000
    # a huge fee over many periods is refused as a suggestion, not crashed on
    q = quote(world, paid_on=D(2026, 3, 17), valid_from=D(2026, 3, 1), valid_to=D(2027, 3, 31), monthly_fee=10_000_000_000)
    assert (q["suggested_amount"], q["basis"]) == (None, "custom")
    # permissions are the same as without it
    url = f"{API}/org/memberships/{mid(world)}/payments/quote"
    assert teacher_client(world).get(url, params={"paid_on": "2026-03-17", "monthly_fee": 1}).status_code == 403


def test_explanations_name_the_package_in_lessons_mode(world):
    setup_mwf(world, fee=600_000)
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    world.db.get(StudentGroup, mid(world)).monthly_fee = None
    world.db.commit()
    q = quote(world, paid_on=D(2026, 3, 5))
    assert (q["basis"], q["explanation"]) == ("none", "Стоимость пакета не задана")
    q = quote(world, paid_on=D(2026, 3, 5), monthly_fee=0)
    assert q["explanation"] == "Стоимость пакета равна 0 — занятия бесплатные"
    for given in (None, 600_000):
        for count in (None, 5, 12):
            q = quote(world, paid_on=D(2026, 3, 5), lessons_count=count, monthly_fee=given)
            assert "месяц" not in q["explanation"].lower() and "оплата в месяц" not in q["explanation"].lower(), q
    # the calendar wording stays
    set_settings(world, period_mode="calendar", billing_anchor="payment_date")
    assert quote(world, paid_on=D(2026, 3, 5))["explanation"] == "Оплата в месяц не задана"


def test_quote_zero_fee_is_free(world):
    setup_mwf(world, fee=0, joined=datetime(2026, 3, 15, 6, 0))
    q = quote(world, paid_on=D(2026, 3, 17))
    assert q["suggested_amount"] == 0 and q["explanation"] == "Оплата в месяц равна 0 — занятия бесплатные"


def test_quote_failures_are_the_same_422_as_creating_the_payment(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    cases = [
        {"paid_on": day(5).isoformat()},
        {"paid_on": "2020-01-01"},
        {"paid_on": "2026-03-17", "valid_from": "2026-03-20", "valid_to": "2026-03-10"},
        {"paid_on": "2026-03-17", "valid_to": "2026-03-31", "lessons_count": 3},
        {"paid_on": "2026-03-17", "valid_from": "2026-03-01", "valid_to": "2027-12-31"},
        {"paid_on": "2026-03-17", "valid_from": "2030-01-01"},
    ]
    for body in cases:
        r_quote = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote", params=body)
        r_create = owner(world).post(f"{API}/org/memberships/{mid(world)}/payments", json={"amount": 1, **body})
        assert r_quote.status_code == r_create.status_code == 422, (body, r_quote.text, r_create.text)
        assert r_quote.json()["detail"] == r_create.json()["detail"], body
    assert owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote").status_code == 422   # paid_on required
    assert owner(world).get(f"{API}/org/memberships/{uuid4()}/payments/quote",
                            params={"paid_on": "2026-03-17"}).status_code == 404
    assert owner(world).get(f"{API}/org/memberships/{mid(world)}/payments/quote",
                            params={"paid_on": "2026-03-17", "lessons_count": 0}).status_code == 422


def test_quote_writes_nothing(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    pay(world, paid_on=D(2026, 3, 17), amount=242_000)
    statements = []
    engine = world.db.get_bind()

    def spy(conn, cursor, statement, *_):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        quote(world, paid_on=D(2026, 4, 2))
        quote(world, paid_on=D(2026, 3, 17), valid_to=D(2026, 4, 30))
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements), statements
    assert not any("FOR UPDATE" in s.upper() or "FOR NO KEY UPDATE" in s.upper() for s in statements)
    assert not world.db.new and not world.db.dirty and not world.db.deleted
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 1


def test_quote_runs_a_bounded_number_of_queries(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    counts = []
    engine = world.db.get_bind()
    event.listen(engine, "before_cursor_execute", lambda *a: counts.append(1))
    quote(world, paid_on=D(2026, 3, 17))
    one = len(counts)
    counts.clear()
    quote(world, paid_on=D(2026, 3, 17), valid_to=D(2026, 5, 31))      # two more periods, one incomplete at each end
    assert len(counts) <= one + 12 and one <= 30, (one, len(counts))


def test_quote_permissions(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    params = {"paid_on": "2026-03-17"}
    url = f"{API}/org/memberships/{mid(world)}/payments/quote"
    assert teacher_client(world).get(url, params=params).status_code == 403
    assert make_client(world, user=world.teacher2).get(url, params=params).status_code == 403
    assert make_client(world, user=world.admin).get(url, params=params).status_code == 403     # no right yet
    assert as_admin(world).get(url, params=params).status_code == 200
    assert make_client(world, user=world.foreign_admin, org="other", org_role="admin").get(
        url, params=params).status_code == 403                                                  # not enabled there
    foreign_owner = make_user(world.db, "owner")
    make_member(world.db, world.other_org, foreign_owner, "owner")
    world.db.commit()
    assert make_client(world, user=foreign_owner, org="other", org_role="owner").get(
        url, params=params).status_code == 404                                                  # someone else's membership
    assert make_client(world, user=world.owner, org="none").get(url, params=params).status_code in (401, 403)


# ---- payments: partial_period, warnings -----------------------------------------------


def test_partial_period_is_stored_with_the_payment_and_survives_a_settings_change(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    partial = pay(world, paid_on=D(2026, 3, 17), amount=242_000)["payment"]
    whole = pay(world, paid_on=D(2026, 4, 2), amount=450_000)["payment"]
    assert (partial["partial_period"], whole["partial_period"]) == (True, False)
    assert (whole["valid_from"], whole["valid_to"]) == ("2026-04-01", "2026-04-30")
    set_settings(world, billing_anchor="payment_date")
    again = {p["id"]: p for p in owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json()}
    assert (again[partial["id"]]["partial_period"], again[whole["id"]]["partial_period"]) == (True, False)
    # entered under "from the payment date": never flagged, however odd the dates
    odd = pay(world, paid_on=D(2026, 5, 12), valid_from=D(2026, 5, 12), valid_to=D(2026, 5, 20), amount=1)["payment"]
    assert odd["partial_period"] is False
    assert stored_payment(world, odd["id"]).partial_period is False


def test_partial_period_on_replace_follows_the_dates_of_the_replacement(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    first = pay(world, paid_on=D(2026, 3, 17), amount=242_000)["payment"]
    assert first["partial_period"] is True
    r = owner(world).post(f"{API}/org/payments/{first['id']}/replace", json={
        "amount": 450_000, "paid_on": "2026-03-17", "valid_from": "2026-03-01", "valid_to": "2026-03-31"})
    assert r.status_code == 201, r.text
    assert r.json()["payment"]["partial_period"] is False
    assert stored_payment(world, first["id"]).partial_period is True      # the voided original keeps its history


def test_lessons_package_and_non_fixed_day_payments_are_never_partial(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    package = pay(world, paid_on=D(2026, 3, 17), lessons_count=8, amount=100)["payment"]
    assert (package["partial_period"], package["valid_to"]) == (False, None)


def test_short_period_at_the_full_fee_gets_a_soft_warning(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    ours = lambda body: [w for w in body["warnings"] if w["code"] == "short_period_full_amount"]  # noqa: E731
    body = pay(world, paid_on=day(), valid_from=D(2026, 3, 15), valid_to=D(2026, 3, 20), amount=450_000)
    assert [w["message"] for w in ours(body)] == ["Короткий период при полной сумме — проверьте даты"]
    assert body["payment"]["amount"] == 450_000                              # saved anyway
    body = pay(world, paid_on=day(), valid_from=D(2026, 3, 21), valid_to=D(2026, 3, 25), amount=100_000)
    assert ours(body) == []                                                  # a prorated amount
    body = pay(world, paid_on=day(), valid_from=D(2026, 3, 26), valid_to=D(2026, 4, 1), amount=450_000)
    assert ours(body) == []                                                  # exactly 7 days
    body = pay(world, paid_on=day(), valid_from=D(2026, 5, 1), valid_to=D(2026, 5, 3), amount=449_999)
    assert ours(body) == []                                                  # below the fee


def test_carry_over_copies_partial_period_and_gets_no_receipt(two):
    two.student = two.students[0]
    first, last = ps.billing_period_start(day(), 1), ps.billing_period_end(day(), 1)
    set_settings(two, billing_anchor="fixed_day", billing_day=1)
    partial = pay(two, paid_on=day(), valid_from=day(-1), valid_to=day(9), amount=100_000)["payment"]
    assert partial["partial_period"] is True and partial["receipt_no"] == 1
    transfer(two, two.second)
    carried = [p for p in owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.second)}/payments").json()]
    assert len(carried) == 1
    assert carried[0]["amount"] == 0 and carried[0]["receipt_no"] is None
    assert carried[0]["partial_period"] is True and carried[0]["carried_from"]["payment_id"] == partial["id"]
    receipt = owner(two).get(f"{API}/org/payments/{carried[0]['id']}/receipt")
    assert receipt.status_code == 409 and receipt.json()["detail"] == "Перенос оплаты — не денежная операция, квитанции нет"
    # a whole-period payment carried over stays not partial; the number keeps counting from the money only
    two.student = two.students[1]
    whole = pay(two, two.student, paid_on=day(), valid_from=first, valid_to=last, amount=5)["payment"]
    assert whole["partial_period"] is False and whole["receipt_no"] == 2
    transfer(two, two.second, student=two.student)
    (carry2,) = owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.second)}/payments").json()
    assert carry2["partial_period"] is False and carry2["receipt_no"] is None
    new = pay(two, two.students[2], paid_on=day(), amount=1)["payment"]
    assert new["receipt_no"] == 3


def test_replacing_the_original_rebuilds_the_carry_over_with_the_new_partial_flag(two):
    two.student = two.students[0]
    set_settings(two, billing_anchor="fixed_day", billing_day=1)
    original = pay(two, paid_on=day(), valid_from=day(-1), valid_to=day(9), amount=100_000)["payment"]
    transfer(two, two.second)
    first, last = ps.billing_period_start(day(), 1), ps.billing_period_end(day(), 1)
    r = owner(two).post(f"{API}/org/payments/{original['id']}/replace", json={
        "amount": 200_000, "paid_on": day().isoformat(), "valid_from": first.isoformat(), "valid_to": last.isoformat()})
    assert r.status_code == 201, r.text
    live = [p for p in owner(two).get(
        f"{API}/org/memberships/{mid(two, two.student, two.second)}/payments",
        params={"include_voided": "false"}).json()]
    assert len(live) == 1 and live[0]["partial_period"] is False and live[0]["receipt_no"] is None
    assert r.json()["payment"]["receipt_no"] == 2                         # the replacement is a new receipt


# ---- receipt numbers ----------------------------------------------------------------------


def test_receipt_numbers_count_inside_the_organisation_and_are_never_reused(world):
    a = pay(world, paid_on=day(-3), valid_to=day(20), amount=1)["payment"]
    b = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20), amount=2)["payment"]
    assert (a["receipt_no"], b["receipt_no"]) == (1, 2)
    # a void keeps the number; the next payment does not take it back
    assert owner(world).post(f"{API}/org/payments/{b['id']}/void", json={"reason": "ошибка"}).status_code == 200
    c = pay(world, world.students[2], paid_on=day(-3), valid_to=day(20), amount=3)["payment"]
    assert c["receipt_no"] == 3
    # a replacement gets a new number, the replaced one keeps its own
    r = owner(world).post(f"{API}/org/payments/{a['id']}/replace",
                          json={"amount": 9, "paid_on": day(-3).isoformat(), "valid_to": day(20).isoformat()})
    assert r.json()["payment"]["receipt_no"] == 4
    assert stored_payment(world, a["id"]).receipt_no == 1
    listing = {p["id"]: p["receipt_no"] for p in owner(world).get(
        f"{API}/org/memberships/{mid(world)}/payments").json()}
    assert sorted(listing.values()) == [1, 4]
    # another centre counts on its own
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="admin")
    other_membership = world.db.query(StudentGroup).filter_by(student_id=world.foreign_student.id).one().id
    world.db.query(type(world.other_org)).filter_by(id=world.other_org.id).update(
        {"settings": {"payments": {"admin_can_manage": True}}})
    world.db.commit()
    r = foreign.post(f"{API}/org/memberships/{other_membership}/payments",
                     json={"amount": 1, "paid_on": day(-1).isoformat(), "valid_to": day(20).isoformat()})
    assert r.status_code == 201 and r.json()["payment"]["receipt_no"] == 1


def test_receipt_number_is_in_the_status_row_and_unique_in_the_database(world):
    created = pay(world, paid_on=day(-2), valid_to=day(20))["payment"]
    assert row_of(world)["last_payment"]["receipt_no"] == created["receipt_no"] == 1
    from sqlalchemy.exc import IntegrityError
    twin = stored_payment(world, created["id"])
    clone = StudentPayment(
        org_id=twin.org_id, membership_id=twin.membership_id, student_id=twin.student_id, group_id=twin.group_id,
        amount=77, paid_on=twin.paid_on, valid_from=twin.valid_from, valid_to=twin.valid_to,
        created_by=world.owner.id, receipt_no=1)
    world.db.add(clone)
    with pytest.raises(IntegrityError, match="uq_student_payments_receipt_no"):
        world.db.flush()
    world.db.rollback()


def test_a_taken_number_is_retried_with_the_new_maximum(world, monkeypatch):
    first = pay(world, paid_on=day(-3), valid_to=day(20), amount=1)["payment"]
    assert first["receipt_no"] == 1
    real = PaymentService._next_receipt_no
    calls = []

    def stale_once(self, org_id):
        calls.append(1)
        return 1 if len(calls) == 1 else real(self, org_id)          # the first attempt sees an old maximum

    monkeypatch.setattr(PaymentService, "_next_receipt_no", stale_once)
    second = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20), amount=2)["payment"]
    assert second["receipt_no"] == 2 and len(calls) == 2
    # the failed attempt left nothing behind and the session is usable
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 2


def test_gives_up_after_ten_conflicts_without_writing_anything(world, monkeypatch):
    pay(world, paid_on=day(-3), valid_to=day(20), amount=1)
    calls = []
    monkeypatch.setattr(PaymentService, "_next_receipt_no", lambda self, org_id: calls.append(1) or 1)
    r = owner(world).post(f"{API}/org/memberships/{mid(world, world.students[1])}/payments",
                          json={"amount": 2, "paid_on": day(-3).isoformat(), "valid_to": day(20).isoformat()})
    assert r.status_code == 409 and "номер квитанции" in r.json()["detail"] and len(calls) == RECEIPT_ATTEMPTS == 10
    monkeypatch.undo()
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 1
    ok = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20), amount=2)["payment"]
    assert ok["receipt_no"] == 2


@pytest.fixture
def engine():
    from sqlalchemy import create_engine
    from src.core.db_timezone import utc_connect_args

    from tests.pg_world import DB_URL

    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    yield engine
    engine.dispose()


def test_concurrent_payments_of_one_centre_get_distinct_numbers(world, engine, monkeypatch):
    """Three requests pick the same number on purpose (a barrier after the max is read); the unique index
    lets one through and the others retry - no number is lost, none is used twice."""
    from sqlalchemy.orm import Session

    real = PaymentService._next_receipt_no
    barrier = threading.Barrier(3, timeout=20)
    local = threading.local()
    reads = []

    def synchronised(self, org_id):
        number = real(self, org_id)
        reads.append(number)
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait()                                          # everybody has read the same maximum
        return number

    monkeypatch.setattr(PaymentService, "_next_receipt_no", synchronised)
    memberships = [mid(world, s) for s in world.students]
    results, errors = [], []

    def worker(membership_id):
        try:
            with Session(engine) as session:
                created = PaymentService(session).create_payment(
                    str(world.org.id), membership_id, str(world.owner.id),
                    PaymentCreate(amount=1000 + len(results), paid_on=day(-2), valid_to=day(20)))
                results.append(created.payment.receipt_no)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(m,)) for m in memberships]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert sorted(results) == [1, 2, 3]
    assert reads[:3] == [0 + 1] * 3 and len(reads) > 3          # all three read the same number; the losers read again
    world.db.expire_all()
    numbers = [p.receipt_no for p in world.db.query(StudentPayment).filter_by(org_id=world.org.id)]
    assert sorted(numbers) == [1, 2, 3]


# ---- receipt ---------------------------------------------------------------------------------


def test_receipt_content(world):
    world.org.name = "ALGO Math Center"
    world.org.city = "Ташкент"
    world.org.phone = "+998901234567"
    world.db.commit()
    p = pay(world, paid_on=day(-3), valid_from=day(-3), valid_to=day(20), amount=1_250_000, note="Наличные")["payment"]
    r = owner(world).get(f"{API}/org/payments/{p['id']}/receipt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "receipt_no": 1, "org_name": "ALGO Math Center", "org_city": "Ташкент", "org_phone": "+998901234567",
        "student_name": "Каримова Алия", "display_id": "N13-001", "group_name": "Группа",
        "amount": 1_250_000, "amount_words": "Один миллион двести пятьдесят тысяч сум", "currency": "UZS",
        "paid_on": day(-3).isoformat(), "valid_from": day(-3).isoformat(), "valid_to": day(20).isoformat(),
        "lessons_count": None, "partial_period": False, "note": "Наличные",
        "accepted_by_name": "Иван Owner", "created_at": p["created_at"], "voided_at": None,
        "void_reason": None, "replaced_by_receipt_no": None, "replaces_receipt_no": None,
    }


def test_receipt_of_a_lessons_package_and_missing_organisation_details(world):
    set_settings(world, period_mode="lessons", lessons_per_payment=8)
    p = pay(world, paid_on=day(-1), amount=500_000)["payment"]
    body = owner(world).get(f"{API}/org/payments/{p['id']}/receipt").json()
    assert (body["lessons_count"], body["valid_to"], body["org_city"], body["org_phone"], body["note"]) == (
        8, None, None, None, None)
    assert body["amount_words"] == "Пятьсот тысяч сум"
    world.org.city, world.org.phone = "  ", ""
    world.db.commit()
    body = owner(world).get(f"{API}/org/payments/{p['id']}/receipt").json()
    assert body["org_city"] is None and body["org_phone"] is None


def test_receipt_of_a_free_payment_and_a_partial_one(world):
    setup_mwf(world, joined=datetime(2026, 3, 15, 6, 0))
    free = pay(world, paid_on=D(2026, 3, 17), amount=0)["payment"]
    body = owner(world).get(f"{API}/org/payments/{free['id']}/receipt").json()
    assert (body["amount"], body["amount_words"], body["partial_period"]) == (0, "Ноль сум", True)


def test_receipt_follows_the_correction_chain_and_shows_the_void(world):
    a = pay(world, paid_on=day(-3), valid_to=day(20), amount=1)["payment"]
    r = owner(world).post(f"{API}/org/payments/{a['id']}/replace", json={
        "amount": 2, "paid_on": day(-3).isoformat(), "valid_to": day(20).isoformat(), "reason": "Неверная сумма"})
    b = r.json()["payment"]
    r = owner(world).post(f"{API}/org/payments/{b['id']}/replace", json={
        "amount": 3, "paid_on": day(-3).isoformat(), "valid_to": day(20).isoformat()})
    c = r.json()["payment"]
    ra, rb, rc = (owner(world).get(f"{API}/org/payments/{p['id']}/receipt").json() for p in (a, b, c))
    assert (ra["receipt_no"], ra["replaced_by_receipt_no"], ra["replaces_receipt_no"]) == (1, 2, None)
    assert (rb["receipt_no"], rb["replaced_by_receipt_no"], rb["replaces_receipt_no"]) == (2, 3, 1)
    assert (rc["receipt_no"], rc["replaced_by_receipt_no"], rc["replaces_receipt_no"]) == (3, None, 2)
    assert ra["voided_at"] and ra["void_reason"] == "Неверная сумма" and rc["voided_at"] is None
    v = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20), amount=5)["payment"]
    owner(world).post(f"{API}/org/payments/{v['id']}/void", json={"reason": "Внесено дважды"})
    rv = owner(world).get(f"{API}/org/payments/{v['id']}/receipt").json()
    assert rv["voided_at"] and rv["void_reason"] == "Внесено дважды" and rv["replaced_by_receipt_no"] is None


def test_receipt_permissions_and_tenancy(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20), amount=1)["payment"]
    url = f"{API}/org/payments/{p['id']}/receipt"
    assert teacher_client(world).get(url).status_code == 403
    assert make_client(world, user=world.admin).get(url).status_code == 403           # right not given
    assert as_admin(world).get(url).status_code == 200                               # given
    assert make_client(world, user=world.foreign_admin, org="other", org_role="admin").get(url).status_code == 403
    foreign_owner = make_user(world.db, "owner")
    make_member(world.db, world.other_org, foreign_owner, "owner")
    world.db.commit()
    assert make_client(world, user=foreign_owner, org="other", org_role="owner").get(url).status_code == 404
    assert owner(world).get(f"{API}/org/payments/{uuid4()}/receipt").status_code == 404


# ---- cheques ---------------------------------------------------------------------------------


def upload(w, payment_id, data=PNG, name="chek.png", client=None, ctype="image/png", expect=200):
    r = (client or owner(w)).post(f"{API}/org/payments/{payment_id}/files", files={"file": (name, data, ctype)})
    assert r.status_code == expect, r.text
    return r.json()


def test_upload_download_and_list_a_cheque(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    png = upload(world, p["id"], PNG, "чек №1.png")
    assert set(png) == {"id", "filename", "content_type", "size_bytes", "uploaded_at", "uploaded_by_name"}
    assert (png["filename"], png["content_type"], png["size_bytes"]) == ("чек №1.png", "image/png", len(PNG))
    assert png["uploaded_by_name"] == "Иван Owner"          # staff: «Имя Фамилия», like teachers on groups
    pdf = upload(world, p["id"], PDF, "scan", ctype="application/octet-stream")          # the client's type is ignored
    assert (pdf["filename"], pdf["content_type"]) == ("scan.pdf", "application/pdf")
    jpg = upload(world, p["id"], JPEG, "x.jpeg", ctype="text/plain")
    assert (jpg["filename"], jpg["content_type"]) == ("x.jpeg", "image/jpeg")
    listing = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json()
    assert [f["id"] for f in listing[0]["files"]] == [png["id"], pdf["id"], jpg["id"]]   # oldest first
    assert "data" not in listing[0]["files"][0]
    # the status row carries the files of the last payment too
    assert [f["id"] for f in row_of(world)["last_payment"]["files"]] == [png["id"], pdf["id"], jpg["id"]]

    r = owner(world).get(f"{API}/org/payments/{p['id']}/files/{png['id']}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-disposition"].startswith("inline;") and "filename*=UTF-8''" in r.headers["content-disposition"]
    assert r.headers["content-security-policy"] == "sandbox"
    r = owner(world).get(f"{API}/org/payments/{p['id']}/files/{pdf['id']}")
    assert r.content == PDF and r.headers["content-type"] == "application/pdf"


def test_the_file_type_comes_from_its_bytes(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    upload(world, p["id"], b"<html><script>alert(1)</script></html>", "x.png", ctype="image/png", expect=415)
    upload(world, p["id"], b"GIF89a....", "x.jpg", ctype="image/jpeg", expect=415)
    upload(world, p["id"], b"PK\x03\x04zip", "x.pdf", ctype="application/pdf", expect=415)
    upload(world, p["id"], b"", "x.png", expect=422)
    assert nfiles(world) == 0
    r = owner(world).post(f"{API}/org/payments/{p['id']}/files", data={"file": "not-a-file"})
    assert r.status_code == 422


def test_size_limit_is_two_megabytes(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    edge = PNG + b"\x00" * (2 * 1024 * 1024 - len(PNG))
    assert len(edge) == 2 * 1024 * 1024
    upload(world, p["id"], edge)                                             # exactly 2 MB is fine
    upload(world, p["id"], edge + b"\x00", expect=413)                       # one byte more is not (read cut off)
    upload(world, p["id"], PNG + b"\x00" * (2 * 1024 * 1024 + 100 * 1024), expect=413)   # refused from the header
    assert nfiles(world) == 1


def test_at_most_three_cheques_per_payment(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    other = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20))["payment"]
    for _ in range(3):
        upload(world, p["id"])
    r = owner(world).post(f"{API}/org/payments/{p['id']}/files", files={"file": ("x.png", PNG, "image/png")})
    assert r.status_code == 409 and r.json()["detail"] == "К оплате можно приложить не более 3 файлов"
    upload(world, other["id"])                                                # another payment has its own three


def test_a_payment_that_cannot_take_a_cheque_is_refused_before_the_body_is_parsed(world, monkeypatch):
    from starlette.requests import Request

    full = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    for _ in range(3):
        upload(world, full["id"])
    voided = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20))["payment"]
    owner(world).post(f"{API}/org/payments/{voided['id']}/void", json={"reason": "ошибка"})

    async def boom(self, *a, **k):
        raise AssertionError("the multipart body must not be parsed")

    monkeypatch.setattr(Request, "form", boom)
    files = {"file": ("x.png", PNG, "image/png")}
    r = owner(world).post(f"{API}/org/payments/{full['id']}/files", files=files)
    assert r.status_code == 409 and r.json()["detail"] == "К оплате можно приложить не более 3 файлов"
    r = owner(world).post(f"{API}/org/payments/{voided['id']}/files", files=files)
    assert r.status_code == 409 and r.json()["detail"] == "Оплата аннулирована — чек прикрепить нельзя"
    assert owner(world).post(f"{API}/org/payments/{uuid4()}/files", files=files).status_code == 404
    foreign_owner = make_user(world.db, "owner")
    make_member(world.db, world.other_org, foreign_owner, "owner")
    world.db.commit()
    fo = make_client(world, user=foreign_owner, org="other", org_role="owner")
    assert fo.post(f"{API}/org/payments/{full['id']}/files", files=files).status_code == 404   # not this centre's
    # the access check still comes first
    assert teacher_client(world).post(f"{API}/org/payments/{full['id']}/files", files=files).status_code == 403
    monkeypatch.undo()
    assert nfiles(world) == 3


def test_precheck_does_not_replace_the_checks_under_the_lock(world):
    """A payment that looks free to the early check can still be refused when the upload reaches the lock."""
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    service = PaymentService(world.db)
    service.precheck_file_upload(str(world.org.id), p["id"])           # fine now
    for _ in range(3):
        upload(world, p["id"])
    r = owner(world).post(f"{API}/org/payments/{p['id']}/files", files={"file": ("x.png", PNG, "image/png")})
    assert r.status_code == 409
    # a malformed body on a good payment is still the 422 of the parser
    q = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20))["payment"]
    assert owner(world).post(f"{API}/org/payments/{q['id']}/files", data={"file": "nope"}).status_code == 422


def test_concurrent_uploads_cannot_exceed_the_limit(world, engine):
    from sqlalchemy.orm import Session

    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    outcomes = []

    def worker():
        with Session(engine) as session:
            try:
                PaymentService(session).add_file(str(world.org.id), p["id"], str(world.owner.id), PNG, "c.png")
                outcomes.append("ok")
            except PaymentConflict as exc:
                outcomes.append("full" if "не более" in exc.message else exc.message)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes) == ["full"] * 3 + ["ok"] * 3
    assert nfiles(world) == 3


def test_no_cheque_on_a_voided_payment_or_a_carry_over(two):
    two.student = two.students[0]
    p = pay(two, paid_on=day(-3), valid_from=day(-3), valid_to=day(20))["payment"]
    kept = upload(two, p["id"])
    transfer(two, two.second)
    (carry,) = owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.second)}/payments").json()
    r = owner(two).post(f"{API}/org/payments/{carry['id']}/files", files={"file": ("x.png", PNG, "image/png")})
    assert r.status_code == 409 and "перенос" in r.json()["detail"].lower()
    assert owner(two).post(f"{API}/org/payments/{p['id']}/void", json={"reason": "ошибка"}).status_code == 200
    r = owner(two).post(f"{API}/org/payments/{p['id']}/files", files={"file": ("x.png", PNG, "image/png")})
    assert r.status_code == 409 and r.json()["detail"] == "Оплата аннулирована — чек прикрепить нельзя"
    # the cheque of a voided payment stays readable (and removable)
    assert owner(two).get(f"{API}/org/payments/{p['id']}/files/{kept['id']}").content == PNG
    listing = owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.group)}/payments").json()
    assert [f["id"] for f in listing[0]["files"]] == [kept["id"]]


def test_download_headers_cannot_be_injected_through_the_filename(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    f = upload(world, p["id"], PNG, 'x";\r\nSet-Cookie: a=b\r\n\r\n<script>.png')
    assert "\r" not in f["filename"] and "\n" not in f["filename"] and '"' not in f["filename"]
    r = owner(world).get(f"{API}/org/payments/{p['id']}/files/{f['id']}")
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert "\r" not in r.headers["content-disposition"] and "\n" not in r.headers["content-disposition"]
    f2 = upload(world, p["id"], PNG, "../../etc/passwd")
    assert f2["filename"] == "passwd.png"
    f3 = upload(world, p["id"], PDF, "Квитанция Отчёт.pdf")
    r = owner(world).get(f"{API}/org/payments/{p['id']}/files/{f3['id']}")
    assert r.status_code == 200 and all(ord(c) < 128 for c in r.headers["content-disposition"])


def test_file_permissions_and_tenancy(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    f = upload(world, p["id"])
    url, furl = f"{API}/org/payments/{p['id']}/files", f"{API}/org/payments/{p['id']}/files/{f['id']}"
    files = {"file": ("x.png", PNG, "image/png")}
    teacher = teacher_client(world)
    assert teacher.post(url, files=files).status_code == 403
    assert teacher.get(furl).status_code == 403
    assert teacher.delete(furl).status_code == 403
    plain_admin = make_client(world, user=world.admin)                          # the owner has not given the right
    assert plain_admin.post(url, files=files).status_code == 403
    assert plain_admin.get(furl).status_code == 403
    assert plain_admin.delete(furl).status_code == 403
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="admin")
    assert foreign.get(furl).status_code == 403
    foreign_owner = make_user(world.db, "owner")
    make_member(world.db, world.other_org, foreign_owner, "owner")
    world.db.commit()
    fo = make_client(world, user=foreign_owner, org="other", org_role="owner")
    assert fo.get(furl).status_code == 404 and fo.delete(furl).status_code == 404
    assert fo.post(url, files=files).status_code == 404
    assert nfiles(world) == 1
    # a file only opens through its own payment
    other = pay(world, world.students[1], paid_on=day(-3), valid_to=day(20))["payment"]
    assert owner(world).get(f"{API}/org/payments/{other['id']}/files/{f['id']}").status_code == 404
    assert owner(world).delete(f"{API}/org/payments/{other['id']}/files/{f['id']}").status_code == 404
    assert owner(world).get(f"{url}/{uuid4()}").status_code == 404


def test_who_may_delete_a_cheque(world):
    p = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    admin = as_admin(world)
    mine = upload(world, p["id"], client=admin)                                  # the administrator's own upload
    theirs = upload(world, p["id"])                                              # the owner's
    furl = lambda f: f"{API}/org/payments/{p['id']}/files/{f['id']}"            # noqa: E731
    assert admin.delete(furl(theirs)).status_code == 403
    assert admin.delete(furl(mine)).json() == {"ok": True}                       # the uploader removes their own
    assert owner(world).delete(furl(theirs)).json() == {"ok": True}              # the owner removes any
    assert nfiles(world) == 0
    assert owner(world).delete(furl(theirs)).status_code == 404
    assert owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json()[0]["files"] == []
    third = upload(world, p["id"], client=admin)
    assert owner(world).delete(furl(third)).status_code == 200                   # the owner removes the administrator's


def test_listing_files_is_one_query_without_the_bytes(world):
    payments = [pay(world, paid_on=day(-3 - i), valid_from=day(-3 - i), valid_to=day(-1 - i), amount=1 + i)["payment"]
                for i in range(1)]
    upload(world, payments[0]["id"])
    statements = []
    engine = world.db.get_bind()

    def spy(conn, cursor, statement, *_):
        statements.append(statement)

    def count_for_list():
        statements.clear()
        event.listen(engine, "before_cursor_execute", spy)
        try:
            r = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments")
        finally:
            event.remove(engine, "before_cursor_execute", spy)
        assert r.status_code == 200
        return len(statements), r.json()

    few, _ = count_for_list()
    for i in range(1, 7):                                   # six more payments, each with two cheques
        p = pay(world, paid_on=day(-3 - i), valid_from=day(-3 - i), valid_to=day(-1 - i), amount=1 + i)["payment"]
        upload(world, p["id"])
        upload(world, p["id"], PDF, "a.pdf")
    many, body = count_for_list()
    assert len(body) == 7 and sum(len(p["files"]) for p in body) == 13
    assert many == few, (few, many)                        # no N+1
    file_selects = [s for s in statements if "student_payment_files" in s]
    assert len(file_selects) == 1 and "student_payment_files.data" not in file_selects[0]


def test_status_page_lists_files_without_extra_queries_per_row(world):
    for student in world.students:
        p = pay(world, student, paid_on=day(-3), valid_to=day(20))["payment"]
        upload(world, p["id"])
    statements = []
    engine = world.db.get_bind()
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *_: statements.append(stmt))
    r = owner(world).get(f"{API}/org/payments/status")
    assert r.status_code == 200
    assert all(len(item["last_payment"]["files"]) == 1 for item in r.json()["items"])
    assert len([s for s in statements if "student_payment_files" in s]) == 1
