"""N13 P2: payments service and API - defaults, duplicates, replace/void, status, tenancy (PostgreSQL)."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from src.auth import payment_status as ps
from src.models.auth import (
    AttendanceExcuse, Group, GroupAttendance, Organization, Student, StudentGroup, StudentPayment,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, give_price, make_client, make_group, make_member, make_student, make_user, needs_pg, pg_session,
    reconcile, save, world,
)

pytestmark = needs_pg

API = "/api/v1/auth"


# ---- helpers ----------------------------------------------------------------------


def mid(w, student=None, group=None):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(
        student_id=(student or w.students[0]).id, group_id=(group or w.group).id
    ).one().id


def day(offset=0):
    return business_today() + timedelta(days=offset)


def owner(w):
    return make_client(w, user=w.owner)


def set_settings(w, **body):
    r = owner(w).put(f"{API}/org/settings/payments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def pay(w, student=None, group=None, client=None, expect=201, **body):
    body.setdefault("amount", 450000)
    body.setdefault("paid_on", day().isoformat())
    for key in ("paid_on", "valid_from", "valid_to"):
        if hasattr(body.get(key), "isoformat"):
            body[key] = body[key].isoformat()
    r = (client or owner(w)).post(f"{API}/org/memberships/{mid(w, student, group)}/payments", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def row_of(w, student=None, group=None):
    r = owner(w).get(f"{API}/org/students/{(student or w.students[0]).id}/memberships")
    assert r.status_code == 200, r.text
    return next(x for x in r.json() if x["group_id"] == str((group or w.group).id))


def status_page(w, **params):
    r = owner(w).get(f"{API}/org/payments/status", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def summary(w):
    r = owner(w).get(f"{API}/org/payments/summary")
    assert r.status_code == 200, r.text
    return r.json()


def backdate(w, payment_id, **fields):
    """Move a stored payment (the API cannot enter a past status without a past date)."""
    w.db.query(StudentPayment).filter_by(id=payment_id).update(fields)
    w.db.commit()


# ---- defaults ---------------------------------------------------------------------


def test_calendar_defaults_period_is_one_month_minus_a_day(world):
    body = pay(world, paid_on=day(-5))
    p = body["payment"]
    assert p["paid_on"] == day(-5).isoformat() and p["valid_from"] == day(-5).isoformat()
    assert p["valid_to"] == ps.add_month_minus_day(day(-5)).isoformat() and p["lessons_count"] is None
    assert p["currency"] == "UZS" and p["created_by"] == str(world.owner.id) and p["voided_at"] is None
    assert p["created_by_name"] == "Иван Owner" and p["amount"] == 450000     # staff: «Имя Фамилия», like teachers on groups
    assert body["warnings"] == []
    assert body["row"]["status"] in ("active", "expiring") and body["row"]["period_mode"] == "calendar"
    assert body["row"]["paid_until"] == p["valid_to"] and body["row"]["last_payment"]["id"] == p["id"]


def test_next_payment_starts_the_day_after_the_previous_paid_until(world):
    first = pay(world, paid_on=day(-20))["payment"]
    nxt = pay(world, paid_on=day(-1))["payment"]  # the old period still runs
    following = (ps.add_month_minus_day(day(-20)) + timedelta(days=1))
    assert nxt["valid_from"] == following.isoformat() and first["valid_to"] < nxt["valid_from"]
    # a payment made after the old period ended closes the debt first: it starts the day after the old
    # "paid until", not on its own date (owner decision 21.10)
    backdate(world, first["id"], valid_from=day(-70), valid_to=day(-40), paid_on=day(-70))
    backdate(world, nxt["id"], valid_from=day(-39), valid_to=day(-10), paid_on=day(-39))
    late = pay(world, paid_on=day(-2))["payment"]
    assert late["valid_from"] == day(-9).isoformat()
    assert late["valid_to"] == ps.add_month_minus_day(day(-9)).isoformat()


# ---- debt: an overdue student closes the debt first (owner decision 21.10) -------------


def only_payment_ends(w, payment_id, valid_to):
    """Make the stored payment a month-long one that ended on ``valid_to`` (the API cannot enter a past status)."""
    start = valid_to - timedelta(days=29)
    backdate(w, payment_id, valid_from=start, valid_to=valid_to, paid_on=start)


def test_overdue_by_days_starts_the_day_after_the_old_paid_until(world):
    first = pay(world, paid_on=day(-1))["payment"]
    only_payment_ends(world, first["id"], day(-13))                  # overdue by 13 days
    assert row_of(world)["status"] == "expired" and row_of(world)["days_overdue"] == 13
    body = pay(world, paid_on=day(0))                                # pays today, no dates sent
    p = body["payment"]
    assert p["valid_from"] == day(-12).isoformat()                    # NOT the payment date
    assert p["valid_to"] == ps.add_month_minus_day(day(-12)).isoformat()
    assert body["row"]["status"] in ("active", "expiring") and body["row"]["paid_until"] == p["valid_to"]


def test_overdue_by_two_months_stays_overdue_after_paying_one_period(world):
    first = pay(world, paid_on=day(-1))["payment"]
    only_payment_ends(world, first["id"], day(-60))
    body = pay(world, paid_on=day(0))
    p = body["payment"]
    assert p["valid_from"] == day(-59).isoformat()
    assert p["valid_to"] == ps.add_month_minus_day(day(-59)).isoformat() and p["valid_to"] < day(0).isoformat()
    assert body["row"]["status"] == "expired" and body["row"]["days_overdue"] > 0     # the debt is not hidden
    again = pay(world, paid_on=day(0), amount=1)["payment"]                           # the next payment continues
    assert again["valid_from"] == (ps.add_month_minus_day(day(-59)) + timedelta(days=1)).isoformat()


def test_a_student_who_left_and_came_back_starts_on_the_return_day(world):
    first = pay(world, paid_on=day(-1))["payment"]
    only_payment_ends(world, first["id"], day(-90))
    membership = world.db.get(StudentGroup, mid(world))
    membership.joined_at = datetime.combine(day(-5), datetime.min.time()).replace(hour=6)
    for stay in membership.periods:
        stay.joined_at = membership.joined_at
    world.db.commit()
    p = pay(world, paid_on=day(0))["payment"]
    assert p["valid_from"] == day(-5).isoformat()                      # days outside the group are never charged


def test_first_payment_does_not_reach_back_to_the_joining_date(world):
    membership = world.db.get(StudentGroup, mid(world))
    membership.joined_at = datetime.combine(day(-300), datetime.min.time()).replace(hour=6)
    world.db.commit()
    p = pay(world, paid_on=day(-1))["payment"]
    assert p["valid_from"] == day(-1).isoformat()


def test_paying_ahead_continues_without_a_gap(world):
    first = pay(world, paid_on=day(-1))["payment"]
    nxt = pay(world, paid_on=day(0), amount=1)["payment"]
    assert nxt["valid_from"] == (date.fromisoformat(first["valid_to"]) + timedelta(days=1)).isoformat()


def test_a_client_sent_start_is_used_as_it_is_even_when_overdue(world):
    first = pay(world, paid_on=day(-1))["payment"]
    only_payment_ends(world, first["id"], day(-13))
    p = pay(world, paid_on=day(0), valid_from=day(-3))["payment"]
    assert p["valid_from"] == day(-3).isoformat()


def test_lessons_package_is_not_moved_by_the_previous_payment(world):
    set_settings(world, period_mode="lessons", lessons_per_payment=8)
    pay(world, paid_on=day(-50))
    p = pay(world, paid_on=day(-1), amount=1)["payment"]
    assert p["valid_from"] == day(-1).isoformat() and p["lessons_count"] == 8


def test_lessons_defaults_use_the_centre_setting(world):
    set_settings(world, period_mode="lessons", lessons_per_payment=8)
    body = pay(world, paid_on=day(-1))
    p = body["payment"]
    assert (p["lessons_count"], p["valid_to"], p["valid_from"]) == (8, None, day(-1).isoformat())
    row = body["row"]
    assert (row["period_mode"], row["status"], row["lessons_total"], row["lessons_used"], row["lessons_left"]) == (
        "lessons", "active", 8, 0, 8)
    assert row["paid_until"] is None and row["days_left"] is None


def test_explicit_period_overrides_the_mode_and_validates(world):
    p = pay(world, paid_on=day(-1), valid_to=day(40))["payment"]
    assert p["valid_to"] == day(40).isoformat()
    set_settings(world, period_mode="lessons")
    p = pay(world, paid_on=day(-1), valid_to=day(60), amount=1)["payment"]       # explicit calendar in lessons centre
    assert p["lessons_count"] is None
    p = pay(world, paid_on=day(-1), lessons_count=5, amount=2)["payment"]
    assert p["lessons_count"] == 5 and p["valid_to"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"paid_on": 1},                                        # in the future (business date)
        {"paid_on": -(3 * 365 + 5)},                            # older than three years
        {"valid_to": 450},                                      # longer than 400 days
        {"valid_to": -3, "valid_from": 0},                      # to before from
        {"valid_to": 30, "lessons_count": 12},                  # both period kinds
        {"amount": -1},
        {"amount": 10_000_000_001},
        {"lessons_count": 0},
        {"lessons_count": 121},
        {"note": "x" * 501},
    ],
)
def test_invalid_payments_are_422_and_write_nothing(world, body):
    for key in ("paid_on", "valid_from", "valid_to"):
        if isinstance(body.get(key), int):
            body[key] = day(body[key])
    pay(world, expect=422, **body)
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 0


def test_warnings_do_not_block(world):
    old = pay(world, paid_on=day(-100), valid_to=day(-75))
    assert [w["code"] for w in old["warnings"]] == ["paid_over_60_days_ago"]
    body = pay(world, paid_on=day(-1), valid_from=day(-80), valid_to=day(-60), amount=2)
    assert [w["code"] for w in body["warnings"]] == ["overlaps_previous"]
    body = pay(world, paid_on=day(0), valid_from=day(30), valid_to=day(100), amount=3)
    assert [w["code"] for w in body["warnings"]] == ["gap_before", "period_over_62_days"]
    assert all(w["message"] for w in body["warnings"])


def test_business_date_is_tashkent_not_utc(world, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(ps, "business_today", lambda tz=None: ps.business_date(
        datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)))
    import src.auth.payments_service as svc
    monkeypatch.setattr(svc.ps, "business_today", ps.business_today)
    assert pay(world, paid_on="2026-10-15", expect=201)["payment"]["paid_on"] == "2026-10-15"
    pay(world, paid_on="2026-10-16", amount=7, expect=422)


# ---- duplicates, replace, void ----------------------------------------------------


def test_duplicate_payment_is_409_in_service_and_unique_index(world):
    first = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    r = owner(world).post(f"{API}/org/memberships/{mid(world)}/payments",
                          json={"amount": 450000, "paid_on": day(-3).isoformat(), "valid_to": day(20).isoformat(),
                                "valid_from": day(-3).isoformat()})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_PAYMENT"
    # the database refuses it too (a concurrent double click that passed the check)
    twin = world.db.get(StudentPayment, first["id"])
    clone = StudentPayment(
        org_id=twin.org_id, membership_id=twin.membership_id, student_id=twin.student_id, group_id=twin.group_id,
        amount=twin.amount, paid_on=twin.paid_on, valid_from=twin.valid_from, valid_to=twin.valid_to,
        created_by=world.owner.id,
    )
    world.db.add(clone)
    with pytest.raises(IntegrityError, match="uq_student_payments_active_dup"):
        world.db.flush()
    world.db.rollback()
    # a different amount is a different payment; a voided twin does not block
    pay(world, paid_on=day(-3), valid_to=day(20), valid_from=day(-3), amount=1)
    assert owner(world).post(f"{API}/org/payments/{first['id']}/void", json={"reason": "ошибка"}).status_code == 200
    pay(world, paid_on=day(-3), valid_to=day(20), valid_from=day(-3))


def test_replace_voids_the_old_payment_and_links_the_new_one(world):
    old = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    r = owner(world).post(f"{API}/org/payments/{old['id']}/replace",
                          json={"amount": 500000, "paid_on": day(-3).isoformat(), "valid_to": day(27).isoformat(),
                                "reason": "Неверная сумма"})
    assert r.status_code == 201, r.text
    body = r.json()
    new = body["payment"]
    assert new["replaces_payment_id"] == old["id"] and new["amount"] == 500000 and new["valid_to"] == day(27).isoformat()
    world.db.expire_all()
    stored = world.db.get(StudentPayment, old["id"])
    assert stored.voided_at is not None and stored.void_reason == "Неверная сумма" and stored.voided_by == world.owner.id
    assert body["row"]["paid_until"] == day(27).isoformat() and body["row"]["last_payment"]["id"] == new["id"]
    # the old one is still there for the history, the live set has one payment
    listing = owner(world).get(f"{API}/org/memberships/{mid(world)}/payments").json()
    assert [p["id"] for p in listing] == [new["id"], old["id"]] or {p["id"] for p in listing} == {new["id"], old["id"]}
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id, voided_at=None).count() == 1


def test_replace_default_reason_and_restating_the_same_figures(world):
    old = pay(world, paid_on=day(-3), valid_to=day(20), valid_from=day(-3))["payment"]
    same = {"amount": 450000, "paid_on": day(-3).isoformat(), "valid_from": day(-3).isoformat(),
            "valid_to": day(20).isoformat()}
    r = owner(world).post(f"{API}/org/payments/{old['id']}/replace", json=same)
    assert r.status_code == 201, r.text  # same figures + a note-less correction is not a duplicate of the voided row
    world.db.expire_all()
    assert world.db.get(StudentPayment, old["id"]).void_reason == "Исправлено"


def test_failed_replace_leaves_the_old_payment_untouched(world):
    old = pay(world, paid_on=day(-3), valid_to=day(20))["payment"]
    r = owner(world).post(f"{API}/org/payments/{old['id']}/replace",
                          json={"amount": 1, "paid_on": day(5).isoformat()})  # future date
    assert r.status_code == 422
    world.db.expire_all()
    assert world.db.get(StudentPayment, old["id"]).voided_at is None
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 1


def test_void_needs_a_reason_and_works_once(world):
    p = pay(world)["payment"]
    client = owner(world)
    for reason in ("", "  ", "ab", "x" * 501):
        assert client.post(f"{API}/org/payments/{p['id']}/void", json={"reason": reason}).status_code == 422
    r = client.post(f"{API}/org/payments/{p['id']}/void", json={"reason": "  Внесено дважды  "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payment"]["void_reason"] == "Внесено дважды" and body["payment"]["voided_by_name"] == "Иван Owner"
    assert body["row"]["status"] == "none" and body["row"]["last_payment"] is None
    r = client.post(f"{API}/org/payments/{p['id']}/void", json={"reason": "ещё раз"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_VOIDED"
    r = client.post(f"{API}/org/payments/{p['id']}/replace", json={"amount": 1, "paid_on": day().isoformat()})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_VOIDED"
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id).count() == 1  # nothing is ever deleted


def test_history_listing_and_include_voided(world):
    a = pay(world, paid_on=day(-40), valid_to=day(-10))["payment"]
    b = pay(world, paid_on=day(-5), valid_to=day(25))["payment"]
    owner(world).post(f"{API}/org/payments/{a['id']}/void", json={"reason": "ошибка"})
    client = owner(world)
    url = f"{API}/org/memberships/{mid(world)}/payments"
    assert [p["id"] for p in client.get(url).json()] == [b["id"], a["id"]]  # newest first
    assert [p["id"] for p in client.get(url, params={"include_voided": "false"}).json()] == [b["id"]]
    assert client.get(f"{API}/org/memberships/{uuid4()}/payments").status_code == 404


# ---- status -----------------------------------------------------------------------


def test_latest_paid_until_wins_and_voided_payments_are_ignored(world):
    long = pay(world, paid_on=day(-30), valid_to=day(40))["payment"]
    pay(world, paid_on=day(-1), valid_from=day(-1), valid_to=day(2), amount=2)  # entered later, ends earlier
    assert row_of(world)["paid_until"] == day(40).isoformat()
    owner(world).post(f"{API}/org/payments/{long['id']}/void", json={"reason": "ошибка"})
    row = row_of(world)
    assert (row["paid_until"], row["status"], row["days_left"]) == (day(2).isoformat(), "expiring", 2)


@pytest.mark.parametrize("offset,status", [(-1, "expired"), (0, "ends_today"), (1, "expiring"), (3, "expiring"), (4, "active")])
def test_status_boundaries_through_the_api(world, offset, status):
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(offset))
    row = row_of(world)
    assert row["status"] == status
    if status == "expired":
        assert row["days_overdue"] == 1
    assert status_page(world, bucket="all")["total"] == 3


def test_free_and_none_statuses(world):
    assert row_of(world)["status"] == "none"
    fee = owner(world).put(f"{API}/org/memberships/{mid(world)}/price-override", json={"amount": 0, "reason": "грант"})
    assert fee.status_code == 200 and fee.json()["row"]["status"] == "free" and fee.json()["row"]["monthly_fee"] == 0
    assert fee.json()["row"]["price"]["amount"] == 0 and fee.json()["row"]["price"]["source"] == "override"
    s = summary(world)
    assert (s["free"], s["active"], s["none"]) == (1, 1, 2)
    assert status_page(world, bucket="active")["items"][0]["status"] == "free"


def test_mode_switch_is_decided_by_the_latest_payment(world):
    pay(world, paid_on=day(-40), valid_to=day(-10))
    assert row_of(world)["status"] == "expired"
    set_settings(world, period_mode="lessons", lessons_per_payment=10)
    pay(world, paid_on=day(-1), amount=2)
    row = row_of(world)
    assert (row["period_mode"], row["status"], row["lessons_total"]) == ("lessons", "active", 10)
    assert row["paid_until"] is None


def lesson_marks(w, offset, marks, reasons=None):
    """A completed lesson of the first group; ``marks`` = {student: mark}."""
    day_, lesson_id = reconcile(w, offset)
    extra = {"excuseReasons": {s.id: r for s, r in (reasons or {}).items()}} if reasons else {}
    full = {s: "present" for s in w.students}      # a lesson cannot be completed with unmarked students
    full.update(marks)
    save(w, day_, lesson_id, list(full.items()), status="completed", **extra)
    return day_, lesson_id


def test_lessons_mode_counts_only_what_rule_43_counts(world):
    s0, s1, _ = world.students
    set_settings(world, period_mode="lessons", lessons_per_payment=3, expiring_lessons=1)
    # lesson 3 days ago: s0 excused (confirmed below), s1 late
    lesson_marks(world, 3, {s0: "excused", s1: "late"}, reasons={s0: "Справка"})
    world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id).update({"status": "confirmed"})
    world.db.commit()
    lesson_marks(world, 2, {s0: "absent", s1: "present"})
    pay(world, s0, paid_on=day(-10), valid_from=day(-10))
    pay(world, s1, paid_on=day(-10), valid_from=day(-10))
    r0, r1 = row_of(world, s0), row_of(world, s1)
    assert (r0["lessons_used"], r0["lessons_left"], r0["status"]) == (1, 2, "active")   # confirmed excuse is free
    assert (r1["lessons_used"], r1["lessons_left"], r1["status"]) == (2, 1, "expiring")
    lesson_marks(world, 1, {s0: "present", s1: "present"})
    assert (row_of(world, s1)["status"], row_of(world, s1)["lessons_left"]) == ("expired", 0)
    assert status_page(world, bucket="due")["total"] == 1


def test_lessons_before_valid_from_and_cancelled_or_open_lessons_do_not_count(world):
    from src.auth.schemas import StartAttendanceRequest
    from tests.pg_world import LIVE_SLOT

    s0 = world.students[0]
    set_settings(world, period_mode="lessons", lessons_per_payment=2)
    lesson_marks(world, 5, {s0: "present"})                 # held before the payment starts
    pay(world, s0, paid_on=day(-2), valid_from=day(-2))
    d, lid = reconcile(world, 1)
    save(world, d, lid, [], status="cancelled", cancellationReason="Праздник")
    assert row_of(world, s0)["lessons_used"] == 0
    # an in-progress lesson does not count either
    world.svc.start_group_attendance(
        str(world.org.id), str(world.group.id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(world.admin.id), is_org_admin=True,
    )
    save(world, business_today(), LIVE_SLOT, [(s0, "present")])
    row = row_of(world, s0)
    assert (row["lessons_used"], row["lessons_left"], row["status"]) == (0, 2, "expiring")


def test_student_not_in_the_roster_snapshot_is_not_charged(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons", lessons_per_payment=5)
    newcomer = make_student(world.db, world.org, world.group, "Новая", "Ученица", "N13-010")
    later = datetime.now() + timedelta(days=1)
    member = world.db.query(StudentGroup).filter_by(student_id=newcomer.id).one()
    member.joined_at = later
    for stay in member.periods:                             # the roster of a lesson asks the periods
        stay.joined_at = later
    world.db.commit()                                       # joins after the lesson: not in its roster
    d, _ = lesson_marks(world, 1, {s0: "present"})
    # A stray mark for somebody who was not in the frozen roster must not spend a lesson.
    att = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=d.isoformat()).one()
    att.records = {**att.records, str(newcomer.id): "present"}
    world.db.commit()
    pay(world, newcomer, paid_on=day(-3), valid_from=day(-3))
    pay(world, s0, paid_on=day(-3), valid_from=day(-3))
    assert row_of(world, newcomer)["lessons_used"] == 0
    assert row_of(world, s0)["lessons_used"] == 1


# ---- tracking ---------------------------------------------------------------------


def test_only_active_memberships_of_live_groups_and_students_are_tracked(world):
    assert summary(world)["tracked_total"] == 3
    world.db.query(Student).filter_by(id=world.students[0].id).update({"status": "archived"})
    world.db.commit()
    assert summary(world)["tracked_total"] == 2
    world.db.query(StudentGroup).filter_by(student_id=world.students[1].id).update({"is_active": False})
    world.db.commit()
    assert summary(world)["tracked_total"] == 1
    world.db.query(Group).filter_by(id=world.group.id).update({"status": "archived"})
    world.db.commit()
    s = summary(world)
    assert s["tracked_total"] == 0 and s["due"] == 0
    # the card of an archived student still shows the payments (for the history)
    assert owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").status_code == 200


def test_student_in_two_groups_is_two_rows_with_their_own_statuses(world):
    second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    world.svc.add_student_to_group(str(world.org.id), str(second.id), str(world.students[0].id), mode="add")
    pay(world, paid_on=day(-30), valid_to=day(-1))
    pay(world, group=second, paid_on=day(-1), valid_to=day(30))
    page = status_page(world, bucket="all", search="Каримова")
    assert page["total"] == 2
    by_group = {r["group_name"]: r for r in page["items"]}
    assert by_group["Группа"]["status"] == "expired" and by_group["Вторая"]["status"] == "active"
    assert by_group["Группа"]["is_primary"] and not by_group["Вторая"]["is_primary"]
    rows = owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").json()
    assert [r["group_name"] for r in rows] == ["Группа", "Вторая"]


def test_memberships_endpoint_hides_closed_memberships_without_payments(world):
    second = make_group(world.db, world.org, world.teacher2, "Вторая")
    third = make_group(world.db, world.org, world.teacher2, "Третья")
    world.db.commit()
    s = world.students[0]
    world.svc.add_student_to_group(str(world.org.id), str(second.id), str(s.id), mode="add")
    world.svc.add_student_to_group(str(world.org.id), str(third.id), str(s.id), mode="add")
    pay(world, paid_on=day(-1))
    pay(world, group=second, paid_on=day(-1))
    for g in (world.group, second, third):                    # leave all, one by one: primary keeps moving
        world.svc.remove_student_from_group(str(world.org.id), str(g.id), str(s.id))
    rows = owner(world).get(f"{API}/org/students/{s.id}/memberships").json()
    assert {r["group_name"] for r in rows} == {"Группа", "Вторая"}  # "Третья" had no payments
    assert all(r["is_active"] is False for r in rows)
    assert owner(world).get(f"{API}/org/students/{uuid4()}/memberships").status_code == 404


def test_closed_membership_keeps_payments_when_the_student_returns(world):
    pay(world, paid_on=day(-2))
    second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    s = world.students[0]
    world.svc.add_student_to_group(str(world.org.id), str(second.id), str(s.id))  # transfer
    world.svc.add_student_to_group(str(world.org.id), str(world.group.id), str(s.id), mode="add")
    row = row_of(world)
    assert row["is_active"] and row["last_payment"] is not None and row["status"] in ("active", "expiring")


# ---- summary == list, ordering, paging --------------------------------------------


def build_mixed(w, extra=0):
    """Students in every state; returns {name: status}."""
    plan = {}
    for i, (name, offset) in enumerate([("Бета", -5), ("Альфа", -1), ("Гамма", 0), ("Дельта", 2), ("Эпсилон", 3), ("Дзета", 30)]):
        st = make_student(w.db, w.org, w.group, name, "Тестов", f"N13-1{i:02d}")
        w.db.commit()
        pay(w, st, paid_on=day(-30), valid_from=day(-30), valid_to=day(offset))
        plan[name] = offset
    free = make_student(w.db, w.org, w.group, "Ноль", "Тестов", "N13-190")
    give_price(w.db, w.db.query(StudentGroup).filter_by(student_id=free.id).one(), 0, reason="грант")
    return plan


def test_summary_equals_the_lists_and_sorting(world):
    build_mixed(world)
    s = summary(world)
    assert s["tracked_total"] == 10 and s["period_mode"] == "calendar" and s["as_of"] == business_today().isoformat()
    assert (s["due"], s["ends_today"], s["expired"], s["expiring"], s["none"], s["active"], s["free"]) == (3, 1, 2, 2, 3, 2, 1)
    assert s["due"] == s["ends_today"] + s["expired"]
    assert s["tracked_total"] == s["due"] + s["expiring"] + s["none"] + s["active"]
    for bucket in ("due", "expiring", "none", "active", "all"):
        page = status_page(world, bucket=bucket)
        assert page["total"] == (s["tracked_total"] if bucket == "all" else s[bucket]), bucket
        assert len(page["items"]) == page["total"] and page["as_of"] == s["as_of"]
    due = [(r["student_name"].split()[1], r["status"]) for r in status_page(world, bucket="due")["items"]]
    assert due == [("Бета", "expired"), ("Альфа", "expired"), ("Гамма", "ends_today")]  # most overdue first
    expiring = [r["student_name"].split()[1] for r in status_page(world, bucket="expiring")["items"]]
    assert expiring == ["Дельта", "Эпсилон"]                                        # fewest days left first
    names = [r["student_name"] for r in status_page(world, bucket="none")["items"]]
    assert names == sorted(names, key=str.lower)


def test_filters_search_and_paging_have_exact_totals(world):
    build_mixed(world)
    second = make_group(world.db, world.org, world.teacher2, "Вторая")
    other = make_student(world.db, world.org, second, "Иван", "Другой", "ZZ-001")
    world.db.commit()
    assert status_page(world, group_id=str(second.id))["total"] == 1
    assert status_page(world, teacher_id=str(world.teacher2.id))["items"][0]["student_name"] == "Другой Иван"
    assert status_page(world, teacher_id=str(world.teacher2.id))["items"][0]["teacher_name"] == "Мария Teacher"
    assert status_page(world, search="zz-001")["total"] == 1
    assert status_page(world, search="Тестов Альфа")["total"] == 1
    assert status_page(world, search="100%")["total"] == 0                       # wildcard characters are literal
    page = status_page(world, limit=4, offset=8)
    assert page["total"] == 11 and len(page["items"]) == 3 and page["limit"] == 4 and page["offset"] == 8
    assert owner(world).get(f"{API}/org/payments/status", params={"limit": 501}).status_code == 422
    assert owner(world).get(f"{API}/org/payments/status", params={"bucket": "bogus"}).status_code == 422


def test_more_than_100_rows_page_with_exact_total_and_no_gaps(world):
    for i in range(130):
        make_student(world.db, world.org, world.group, f"Имя{i:03d}", "Массов", f"BULK-{i:03d}")
    world.db.commit()
    seen = []
    for offset in range(0, 140, 50):
        page = status_page(world, bucket="none", limit=50, offset=offset)
        assert page["total"] == 133
        seen += [r["membership_id"] for r in page["items"]]
    assert len(seen) == len(set(seen)) == 133
    assert status_page(world, bucket="none", limit=500)["total"] == 133 == len(status_page(world, bucket="none", limit=500)["items"])
    assert summary(world)["tracked_total"] == 133


@pytest.mark.parametrize("mode", ["calendar", "lessons"])
def test_status_and_summary_use_a_bounded_number_of_queries(world, mode):
    set_settings(world, period_mode=mode)

    def populate(count):
        for i in range(count):
            st = make_student(world.db, world.org, world.group, f"Н{i}{uuid4().hex[:4]}", "Запрос", f"Q-{uuid4().hex[:8]}")
            world.db.commit()
            body = {"amount": 1 + i, "paid_on": day(-3).isoformat()}
            r = owner(world).post(f"{API}/org/memberships/{mid(world, st)}/payments", json=body)
            assert r.status_code == 201, r.text

    def count_queries(path):
        statements = []
        engine = world.db.get_bind()
        listener = lambda conn, cur, stmt, *a: statements.append(stmt)
        event.listen(engine, "before_cursor_execute", listener)
        try:
            assert owner(world).get(path).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(statements)

    populate(3)
    paths = [f"{API}/org/payments/status?limit=50", f"{API}/org/payments/summary"]
    small = [count_queries(p) for p in paths]
    populate(25)
    large = [count_queries(p) for p in paths]
    assert large == small, (small, large)   # no query per row
    assert max(large) <= 14


# ---- fee, settings, tenancy -------------------------------------------------------


def test_the_old_fee_endpoint_is_gone_and_says_where_to_go(world):
    client = owner(world)
    url = f"{API}/org/memberships/{mid(world)}/fee"
    for body in ({"monthly_fee": 450000}, {"monthly_fee": None}, {"monthly_fee": -1}, {}):
        r = client.put(url, json=body)
        assert r.status_code == 410, (body, r.text)                     # whatever the body: it is gone, not invalid
        detail = r.json()["detail"]
        assert detail["code"] == "FEE_ENDPOINT_GONE"
        assert "/org/groups/{group_id}/price" in detail["message"] and "/org/memberships/{membership_id}/price-override" in detail["message"]
    assert client.put(f"{API}/org/memberships/{uuid4()}/fee", json={"monthly_fee": 1}).status_code == 410
    world.db.expire_all()
    m = world.db.get(StudentGroup, mid(world))
    assert m.monthly_fee is None and m.monthly_fee_updated_at is None   # nothing was written


def test_settings_defaults_patch_semantics_and_audit_stamp(world):
    client = owner(world)
    r = client.get(f"{API}/org/settings/payments")
    assert r.json() == {"period_mode": "calendar", "billing_anchor": "payment_date", "billing_day": 1,
                        "lessons_per_payment": 12, "expiring_days": 3,
                        "expiring_lessons": 3, "excuse_deadline_days": 5, "excuse_deadline_lessons": 2,
                        "admin_can_manage": False, "accounting_start_date": None, "updated_by": None, "updated_at": None}
    world.db.query(Organization).filter_by(id=world.org.id).update({"settings": {"theme": "keep-me"}})
    world.db.commit()
    r = client.put(f"{API}/org/settings/payments", json={"period_mode": "lessons", "expiring_lessons": 5})
    body = r.json()
    assert (body["period_mode"], body["lessons_per_payment"], body["expiring_lessons"]) == ("lessons", 12, 5)
    assert body["updated_by"] == str(world.owner.id) and body["updated_at"]
    r = client.put(f"{API}/org/settings/payments", json={"admin_can_manage": True})
    assert r.json()["period_mode"] == "lessons" and r.json()["admin_can_manage"] is True   # untouched fields stay
    world.db.expire_all()
    stored = world.db.get(Organization, world.org.id).settings
    assert stored["theme"] == "keep-me" and stored["payments"]["admin_can_manage"] is True
    for bad in ({"lessons_per_payment": 0}, {"lessons_per_payment": 61}, {"expiring_days": 15}, {"expiring_lessons": 11},
                {"excuse_deadline_days": -1}, {"excuse_deadline_days": 31}, {"excuse_deadline_lessons": -1},
                {"excuse_deadline_lessons": 13},
                {"period_mode": "weekly"}, {"admin_can_manage": "maybe"},
                {"billing_anchor": "monthly"}, {"billing_day": 0}, {"billing_day": 29}):
        assert client.put(f"{API}/org/settings/payments", json=bad).status_code == 422, bad


def test_expiring_thresholds_come_from_the_settings(world):
    pay(world, paid_on=day(-20), valid_from=day(-20), valid_to=day(5))
    assert row_of(world)["status"] == "active"
    set_settings(world, expiring_days=5)
    assert row_of(world)["status"] == "expiring"


def test_other_organisation_gets_404_everywhere_and_sees_nothing(world):
    p = pay(world)["payment"]
    m = mid(world)
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="owner", role="owner")
    # the foreign "owner" is an admin in the member table; give the test owner role there
    world.db.query(__import__("src.models.auth", fromlist=["OrganizationMember"]).OrganizationMember).filter_by(
        user_id=world.foreign_admin.id).update({"org_role": "owner"})
    world.db.commit()
    day_body = {"amount": 1, "paid_on": day().isoformat()}
    assert foreign.get(f"{API}/org/memberships/{m}/payments").status_code == 404
    assert foreign.post(f"{API}/org/memberships/{m}/payments", json=day_body).status_code == 404
    assert foreign.put(f"{API}/org/memberships/{m}/price-override", json={"amount": 1, "reason": "чужая"}).status_code == 404
    assert foreign.get(f"{API}/org/memberships/{m}/price-override").status_code == 404
    assert foreign.get(f"{API}/org/groups/{world.group.id}/prices").status_code == 404
    assert foreign.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 1}).status_code == 404
    assert foreign.post(f"{API}/org/payments/{p['id']}/replace", json=day_body).status_code == 404
    assert foreign.post(f"{API}/org/payments/{p['id']}/void", json={"reason": "чужая"}).status_code == 404
    assert foreign.get(f"{API}/org/students/{world.students[0].id}/memberships").status_code == 404
    assert foreign.get(f"{API}/org/payments/status", params={"search": "Каримова"}).json()["total"] == 0
    s = foreign.get(f"{API}/org/payments/summary").json()
    assert s["tracked_total"] == 1 and s["due"] == 0       # only its own student
    world.db.expire_all()
    assert world.db.get(StudentPayment, p["id"]).voided_at is None
    # and the settings of one centre never leak into the other
    set_settings(world, period_mode="lessons")
    assert foreign.get(f"{API}/org/settings/payments").json()["period_mode"] == "calendar"


# ---- review follow-ups: default end of period, valid_from bounds, extreme dates ------


@pytest.mark.parametrize(
    "paid_on,expected",
    [
        ("2026-01-31", "2026-02-28"), ("2026-10-01", "2026-10-31"), ("2028-02-29", "2028-03-28"),
        ("2026-11-30", "2026-12-29"), ("2026-03-31", "2026-04-30"), ("2026-10-05", "2026-11-04"),
    ],
)
def test_default_end_of_period_over_the_api(world, paid_on, expected, monkeypatch):
    from datetime import datetime, timezone
    import src.auth.payments_service as svc
    monkeypatch.setattr(svc.ps, "business_today", lambda tz=None: date.fromisoformat(paid_on))
    assert pay(world, paid_on=paid_on)["payment"]["valid_to"] == expected


def test_valid_from_must_be_within_three_years_back_and_400_days_forward(world):
    paid = day(-1)
    far_back = (paid.replace(year=paid.year - 3) - timedelta(days=1)).isoformat()
    pay(world, paid_on=paid, valid_from=far_back, expect=422)
    pay(world, paid_on=paid, valid_from=(paid + timedelta(days=401)).isoformat(), expect=422)
    pay(world, paid_on=paid, valid_from=(paid + timedelta(days=401)).isoformat(), lessons_count=5, expect=422)
    pay(world, paid_on=paid, valid_from=(paid + timedelta(days=400)).isoformat(), valid_to=(paid + timedelta(days=430)).isoformat(), amount=2)
    start = paid.replace(year=paid.year - 3)
    ok = pay(world, paid_on=paid, valid_from=start.isoformat(), valid_to=(start + timedelta(days=30)).isoformat(), amount=3)
    assert ok["payment"]["valid_from"] == start.isoformat()


EXTREMES = ["9999-12-31", "9999-12-30", "0001-01-01", "0001-01-02", "2999-01-01"]


@pytest.mark.parametrize("extreme", EXTREMES)
@pytest.mark.parametrize("mode", ["calendar", "lessons"])
def test_extreme_dates_are_422_never_500(world, extreme, mode):
    set_settings(world, period_mode=mode)
    base = {"amount": 1}
    client = owner(world)
    url = f"{API}/org/memberships/{mid(world)}/payments"
    today = day().isoformat()
    for body in (
        {**base, "paid_on": extreme},
        {**base, "paid_on": today, "valid_from": extreme},
        {**base, "paid_on": today, "valid_to": extreme},
        {**base, "paid_on": today, "valid_from": extreme, "valid_to": extreme},
        {**base, "paid_on": today, "valid_from": extreme, "lessons_count": 3},
        {**base, "paid_on": extreme, "valid_from": extreme},
    ):
        r = client.post(url, json=body)
        assert r.status_code == 422, (body, r.status_code, r.text[:200])
    p = pay(world)["payment"]
    for path, body in (
        (f"{API}/org/payments/{p['id']}/replace", {**base, "paid_on": extreme}),
        (f"{API}/org/payments/{p['id']}/replace", {**base, "paid_on": today, "valid_from": extreme}),
    ):
        assert client.post(path, json=body).status_code == 422
    assert world.db.query(StudentPayment).filter_by(org_id=world.org.id, voided_at=None).count() == 1


def test_a_previous_payment_ending_at_the_edge_does_not_break_the_next_default(world):
    first = pay(world, paid_on=day(-1), valid_from=day(-1), valid_to=day(399))["payment"]
    nxt = pay(world, paid_on=day(0), amount=2)["payment"]       # default start = day after 399
    assert nxt["valid_from"] == day(400).isoformat() and nxt["valid_to"]


def test_summary_carries_lessons_per_payment(world):
    assert summary(world)["lessons_per_payment"] == 12
    set_settings(world, period_mode="lessons", lessons_per_payment=8)
    s = summary(world)
    assert (s["lessons_per_payment"], s["period_mode"]) == (8, "lessons")
