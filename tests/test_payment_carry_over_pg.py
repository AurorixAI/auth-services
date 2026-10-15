"""N13 P2.1: the payment follows the student on a transfer (PostgreSQL)."""
import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

from uuid import uuid4

import pytest

from src.auth.schemas import SaveAttendanceRequest, StartAttendanceRequest
from src.models.auth import GroupAttendance, Organization, Student, StudentGroup, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, make_client, make_group, make_student, needs_pg, pg_session, reconcile, world,
)
from tests.test_payments_service_pg import (  # noqa: F401
    API, day, lesson_marks, mid, owner, pay, row_of, set_settings, status_page, summary,
)

pytestmark = needs_pg

GROUPS = f"{API}/org/groups"
SECRET = 7654321


@pytest.fixture
def two(world):
    """The world plus a second group; ``student`` sits in the first one."""
    world.second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.third = make_group(world.db, world.org, world.teacher2, "Третья")
    world.db.commit()
    world.student = world.students[0]
    return world


# ---- helpers ----------------------------------------------------------------------


def transfer(w, group, student=None, client=None, expect=200, body=None, **params):
    s = student or w.student
    r = (client or owner(w)).post(f"{GROUPS}/{group.id}/students/{s.id}", params=params or None, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def carry_warnings(result):
    return [x for x in result["warnings"] if x["code"] == "payment_carried_over"]


def history(w, group, student=None, include_voided=True):
    r = owner(w).get(
        f"{API}/org/memberships/{mid(w, student or w.student, group)}/payments",
        params={"include_voided": include_voided},
    )
    assert r.status_code == 200, r.text
    return r.json()


def stored(w, **filters):
    w.db.expire_all()
    return w.db.query(StudentPayment).filter_by(**filters).order_by(StudentPayment.created_at).all()


def membership(w, group, student=None):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(group_id=group.id, student_id=(student or w.student).id).one()


@contextmanager
def clock(monkeypatch, day_, hour, minute=0):
    """The membership service sees "now" as ``day_`` hour:minute Tashkent time (a transfer in the past)."""
    from src.auth import service as service_module
    from src.auth.service import BUSINESS_TIMEZONE

    instant = datetime.combine(day_, time(hour, minute), tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    with monkeypatch.context() as patch:
        patch.setattr(service_module, "datetime", Frozen)
        yield instant


def add_slot(w, group, day_, slot_id, start, end):
    """One more weekly slot (e.g. 17:00) on the weekday of ``day_``."""
    group.schedule = [*group.schedule, {
        "id": slot_id, "dayOfWeek": day_.isoweekday(), "startTime": start, "endTime": end, "format": "offline"}]
    w.db.commit()


def hold(w, group, day_, slot_id):
    """A completed lesson of ``group`` on ``day_``; everybody on its frozen roster is present."""
    w.svc.reconcile_missed_group_attendance(
        str(w.org.id), str(group.id), StartAttendanceRequest(lessonDate=day_, lessonId=slot_id),
        reconciled_by=str(w.admin.id), is_org_admin=True,
    )
    w.db.expire_all()
    att = w.db.query(GroupAttendance).filter_by(group_id=group.id, lesson_date=day_.isoformat(), lesson_id=slot_id).one()
    roster = [item["student_id"] for item in att.roster_snapshot]
    w.svc.save_group_attendance(
        str(w.org.id), str(group.id),
        SaveAttendanceRequest(lessonDate=day_, lessonId=slot_id, status="completed", records={sid: "present" for sid in roster}),
        marked_by=str(w.admin.id), is_org_admin=True, is_org_manager=True,
    )
    return roster


def complete_in(w, group, offset_days):
    """A completed past lesson of ``group`` where its only student is present."""
    day_, lesson_id = reconcile(w, offset_days, group=group)
    w.svc.save_group_attendance(
        str(w.org.id), str(group.id),
        SaveAttendanceRequest(lessonDate=day_, lessonId=lesson_id, status="completed",
                              records={str(w.student.id): "present"}),
        marked_by=str(w.admin.id), is_org_admin=True, is_org_manager=True,
    )


def set_fee(w, group, fee, student=None):
    r = owner(w).put(f"{API}/org/memberships/{mid(w, student or w.student, group)}/fee", json={"monthly_fee": fee})
    assert r.status_code == 200, r.text


# ---- calendar -----------------------------------------------------------------------


def test_calendar_payment_is_carried_with_the_same_end(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20), amount=SECRET)["payment"]
    result = transfer(two, two.second)
    assert result["status"] == "added"
    (note,) = carry_warnings(result)
    assert note["message"] == f"Оплата до {day(20).strftime('%d.%m')} перенесена в «Вторая»"
    assert str(SECRET) not in json.dumps(result)

    (carried,) = history(two, two.second)
    assert (carried["amount"], carried["currency"]) == (0, "UZS")
    assert carried["paid_on"] == day(-10).isoformat()
    assert (carried["valid_from"], carried["valid_to"], carried["lessons_count"]) == (
        day(0).isoformat(), day(20).isoformat(), None)
    assert carried["note"] == "Перенос оплаты из «Группа»"
    assert carried["carried_from"] == {"payment_id": original["id"], "group_name": "Группа"}
    assert carried["created_by"] == str(two.owner.id) and carried["voided_at"] is None

    row = row_of(two, group=two.second)
    assert (row["status"], row["paid_until"], row["period_mode"]) == ("active", day(20).isoformat(), "calendar")
    # the original stays on the closed group, live, but stamped: it covers only the days before the move
    (kept,) = history(two, two.group)
    assert kept["id"] == original["id"] and kept["amount"] == SECRET and kept["voided_at"] is None
    assert kept["carried_from"] is None and carried["carried_out"] is None
    assert kept["carried_out"] == {"date": day(0).isoformat(), "group_name": "Вторая"}
    closed = row_of(two, group=two.group)       # day T itself stays covered: a lesson there started before the move
    assert (closed["status"], closed["paid_until"]) == ("ends_today", day(0).isoformat())


def test_last_payment_of_the_row_is_the_latest_even_when_it_is_a_carry_over(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20), amount=SECRET)
    transfer(two, two.second)
    last = row_of(two, group=two.second)["last_payment"]
    assert last["amount"] == 0 and last["carried_from"]["group_name"] == "Группа"
    # a real payment entered later wins (newer paid_on) and has no source
    pay(two, group=two.second, paid_on=day(0), amount=123456)
    last = row_of(two, group=two.second)["last_payment"]
    assert last["amount"] == 123456 and last["carried_from"] is None


def test_prepaid_future_period_keeps_its_start(two):
    pay(two, paid_on=day(0), valid_from=day(5), valid_to=day(35))
    transfer(two, two.second)
    (carried,) = history(two, two.second)
    assert (carried["valid_from"], carried["valid_to"]) == (day(5).isoformat(), day(35).isoformat())


def test_payment_ending_today_is_carried_but_yesterday_is_not(two):
    s0, s1 = two.students[:2]
    pay(two, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    pay(two, s1, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    r0 = transfer(two, two.second, s0)
    r1 = transfer(two, two.second, s1)
    assert len(carry_warnings(r0)) == 1 and carry_warnings(r1) == []
    assert row_of(two, s0, two.second)["status"] == "ends_today"
    assert history(two, two.second, s1) == []
    assert row_of(two, s1, two.second)["status"] == "none"


def test_several_payments_in_force_are_all_carried_expired_ones_are_not(two):
    old = pay(two, paid_on=day(-50), valid_from=day(-50), valid_to=day(-20))["payment"]
    now = pay(two, paid_on=day(-15), valid_from=day(-15), valid_to=day(14), amount=2)["payment"]
    nxt = pay(two, paid_on=day(-1), valid_to=day(44), amount=3)["payment"]      # renewal paid in advance
    result = transfer(two, two.second)
    assert len(carry_warnings(result)) == 2
    sources = {h["carried_from"]["payment_id"] for h in history(two, two.second)}
    assert sources == {now["id"], nxt["id"]} and old["id"] not in sources
    assert row_of(two, group=two.second)["paid_until"] == day(44).isoformat()


# ---- lessons --------------------------------------------------------------------------


def test_lessons_package_remainder_is_carried_after_the_fifo(two):
    set_settings(two, period_mode="lessons", lessons_per_payment=2)
    first = pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=2, amount=1)["payment"]
    second = pay(two, paid_on=day(-6), valid_from=day(-6), lessons_count=4, amount=SECRET)["payment"]
    for offset in (5, 4, 3):                                    # 3 lessons: 2 go to the first package, 1 to the second
        lesson_marks(two, offset, {})
    before = row_of(two)
    assert (before["lessons_total"], before["lessons_used"], before["lessons_left"]) == (6, 3, 3)

    result = transfer(two, two.second)
    (note,) = carry_warnings(result)
    assert note["message"] == "Остаток пакета — 3 урока — перенесён в «Вторая»"
    assert str(SECRET) not in json.dumps(result)
    (carried,) = history(two, two.second)              # the exhausted first package is not carried
    assert (carried["lessons_count"], carried["valid_to"], carried["amount"]) == (3, None, 0)
    assert carried["valid_from"] == day(0).isoformat() and carried["paid_on"] == day(-6).isoformat()
    assert carried["carried_from"]["payment_id"] == second["id"] and first["id"] != second["id"]
    row = row_of(two, group=two.second)
    assert (row["period_mode"], row["lessons_total"], row["lessons_used"], row["lessons_left"], row["status"]) == (
        "lessons", 3, 0, 3, "expiring")             # 3 left = the default "soon" threshold


def test_exhausted_package_is_not_carried(two):
    set_settings(two, period_mode="lessons", lessons_per_payment=2)
    pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=2)
    for offset in (4, 3):
        lesson_marks(two, offset, {})
    assert row_of(two)["lessons_left"] == 0
    assert carry_warnings(transfer(two, two.second)) == []
    assert history(two, two.second) == []


def test_plural_forms_of_the_lessons_message(two):
    from src.auth.payments_service import _plural_lessons

    assert [_plural_lessons(n) for n in (1, 2, 4, 5, 11, 12, 14, 21, 22, 25)] == [
        "урок", "урока", "урока", "уроков", "уроков", "уроков", "уроков", "урок", "урока", "уроков"]


def test_only_the_mode_of_the_latest_payment_is_carried(two):
    pay(two, paid_on=day(-20), valid_from=day(-20), lessons_count=5, amount=1)       # superseded by the calendar one
    pay(two, paid_on=day(-1), valid_from=day(-1), valid_to=day(29), amount=2)
    transfer(two, two.second)
    (carried,) = history(two, two.second)
    assert carried["valid_to"] == day(29).isoformat() and carried["lessons_count"] is None


# ---- when nothing moves -----------------------------------------------------------------


def test_add_mode_never_carries_and_never_copies_the_fee(two):
    set_fee(two, two.group, 300000)
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = transfer(two, two.second, mode="add")
    assert carry_warnings(result) == []
    assert history(two, two.second) == [] and len(history(two, two.group)) == 1
    assert membership(two, two.second).monthly_fee is None
    assert membership(two, two.group).is_active is True


def test_transfer_without_payments_adds_nothing(two):
    result = transfer(two, two.second)
    assert result["warnings"] == [] and history(two, two.second) == []


def test_unchanged_transfer_is_a_no_op(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = transfer(two, two.group)
    assert result["status"] == "unchanged" and result["warnings"] == []
    assert len(history(two, two.group)) == 1


def test_internal_call_without_an_actor_carries_nothing(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = two.svc.add_student_to_group(str(two.org.id), str(two.second.id), str(two.student.id))
    assert result["status"] == "added" and result["warnings"] == []
    assert stored(two, membership_id=membership(two, two.second).id) == []


# ---- bulk -----------------------------------------------------------------------------------


def test_bulk_transfer_carries_per_student(two):
    s0, s1, s2 = two.students
    pay(two, s0, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    pay(two, s1, paid_on=day(-10), valid_from=day(-10), valid_to=day(-2))          # expired: nothing
    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, s2, paid_on=day(-10), valid_from=day(-10))
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(s.id) for s in two.students]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 3
    carried = {w["student_id"]: w["message"] for w in body["warnings"] if w["code"] == "payment_carried_over"}
    assert carried == {
        str(s0.id): f"Оплата до {day(20).strftime('%d.%m')} перенесена в «Вторая»",
        str(s2.id): "Остаток пакета — 6 уроков — перенесён в «Вторая»",
    }
    assert history(two, two.second, s1) == []
    assert row_of(two, s0, two.second)["status"] == "active" and row_of(two, s2, two.second)["lessons_left"] == 6


def test_bulk_add_mode_carries_nothing(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", params={"mode": "add"},
                        json={"student_ids": [str(two.student.id)]})
    assert r.status_code == 200 and r.json()["warnings"] == []
    assert history(two, two.second) == []


# ---- fee -------------------------------------------------------------------------------------


def test_fee_is_copied_when_the_new_group_has_none(two):
    set_fee(two, two.group, 300000)
    transfer(two, two.second)
    m = membership(two, two.second)
    assert m.monthly_fee == 300000 and m.monthly_fee_updated_by == two.owner.id and m.monthly_fee_updated_at
    assert membership(two, two.group).monthly_fee == 300000          # the old one keeps its own


def test_explicit_fee_wins_over_the_copy(two):
    set_fee(two, two.group, 300000)
    transfer(two, two.second, body={"monthly_fee": 500000})
    assert membership(two, two.second).monthly_fee == 500000


def test_existing_fee_of_the_new_group_is_kept_and_a_free_group_copies_zero(two):
    s0, s1 = two.students[:2]
    set_fee(two, two.group, 300000, s0)
    two.db.add(StudentGroup(student_id=s0.id, group_id=two.second.id, is_active=False,
                            joined_at=datetime.now() - timedelta(days=90), monthly_fee=777000))
    two.db.commit()
    transfer(two, two.second, s0)                                     # reactivated membership keeps 777000
    assert membership(two, two.second, s0).monthly_fee == 777000
    set_fee(two, two.group, 0, s1)
    transfer(two, two.second, s1)
    assert membership(two, two.second, s1).monthly_fee == 0           # "free" is a fee that is set


def test_no_fee_on_either_side_stays_empty(two):
    transfer(two, two.second)
    assert membership(two, two.second).monthly_fee is None


# ---- cascades --------------------------------------------------------------------------------


def test_voiding_the_original_voids_its_carry_overs(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    r = owner(two).post(f"{API}/org/payments/{original['id']}/void", json={"reason": "Ошибка кассира"})
    assert r.status_code == 200, r.text
    (carried,) = history(two, two.second)
    assert carried["voided_at"] and carried["void_reason"] == "Исходная оплата отменена"
    assert carried["voided_by"] == str(two.owner.id)
    assert row_of(two, group=two.second)["status"] == "none"
    assert history(two, two.second, include_voided=False) == []


def test_voiding_follows_the_whole_chain(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    transfer(two, two.third)                                          # carry-over of a carry-over
    (third,) = history(two, two.third)
    (second,) = history(two, two.second)
    assert third["carried_from"] == {"payment_id": second["id"], "group_name": "Вторая"}
    assert third["note"] == "Перенос оплаты из «Вторая»"
    assert owner(two).post(f"{API}/org/payments/{original['id']}/void", json={"reason": "Ошибка"}).status_code == 200
    assert history(two, two.second, include_voided=False) == [] and history(two, two.third, include_voided=False) == []


def test_voiding_a_carry_over_directly_leaves_the_original(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carried,) = history(two, two.second)
    r = owner(two).post(f"{API}/org/payments/{carried['id']}/void", json={"reason": "Не нужен"})
    assert r.status_code == 200 and r.json()["payment"]["carried_from"]["group_name"] == "Группа"
    assert history(two, two.group)[0]["voided_at"] is None and history(two, two.group)[0]["id"] == original["id"]
    assert row_of(two, group=two.second)["status"] == "none"


def test_replacing_the_original_replaces_the_carry_over(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (old_carry,) = history(two, two.second)
    r = owner(two).post(
        f"{API}/org/payments/{original['id']}/replace",
        json={"amount": 450000, "paid_on": day(-10).isoformat(), "valid_from": day(-10).isoformat(),
              "valid_to": day(25).isoformat()},
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["payment"]["id"]
    live = history(two, two.second, include_voided=False)
    (new_carry,) = live
    assert new_carry["id"] != old_carry["id"]
    assert (new_carry["valid_from"], new_carry["valid_to"], new_carry["amount"]) == (
        old_carry["valid_from"], day(25).isoformat(), 0)
    assert new_carry["carried_from"]["payment_id"] == new_id
    voided = [h for h in history(two, two.second) if h["id"] == old_carry["id"]][0]
    assert voided["voided_at"] and voided["void_reason"] == "Исходная оплата исправлена"
    assert row_of(two, group=two.second)["paid_until"] == day(25).isoformat()
    (corrected,) = history(two, two.group, include_voided=False)           # the correction keeps the stamp
    assert corrected["id"] == new_id and corrected["carried_out"] == {"date": day(0).isoformat(), "group_name": "Вторая"}
    assert two.db.get(StudentPayment, new_carry["id"]).carried_out_at is None


def test_replacement_that_ended_before_the_carry_over_starts_leaves_none(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    r = owner(two).post(
        f"{API}/org/payments/{original['id']}/replace",
        json={"amount": 450000, "paid_on": day(-10).isoformat(), "valid_from": day(-10).isoformat(),
              "valid_to": day(-3).isoformat()},
    )
    assert r.status_code == 201, r.text
    assert history(two, two.second, include_voided=False) == []
    assert row_of(two, group=two.second)["status"] == "none"
    (corrected,) = history(two, two.group, include_voided=False)           # nothing moved after all: no stamp
    assert corrected["carried_out"] is None


def test_replacing_a_lessons_original_recomputes_the_remainder(two):
    set_settings(two, period_mode="lessons", lessons_per_payment=5)
    original = pay(two, paid_on=day(-8), valid_from=day(-8), lessons_count=5)["payment"]
    lesson_marks(two, 3, {})
    lesson_marks(two, 2, {})
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    assert carry["lessons_count"] == 3
    r = owner(two).post(
        f"{API}/org/payments/{original['id']}/replace",
        json={"amount": 1, "paid_on": day(-8).isoformat(), "valid_from": day(-8).isoformat(), "lessons_count": 8},
    )
    assert r.status_code == 201, r.text
    (again,) = history(two, two.second, include_voided=False)
    assert again["lessons_count"] == 6 and again["carried_from"]["payment_id"] == r.json()["payment"]["id"]
    assert row_of(two, group=two.second)["lessons_left"] == 6


def test_replacing_the_original_rebuilds_the_whole_chain(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    transfer(two, two.third)
    r = owner(two).post(
        f"{API}/org/payments/{original['id']}/replace",
        json={"amount": 5, "paid_on": day(-10).isoformat(), "valid_from": day(-10).isoformat(), "valid_to": day(30).isoformat()},
    )
    assert r.status_code == 201, r.text
    (second,) = history(two, two.second, include_voided=False)
    (third,) = history(two, two.third, include_voided=False)
    assert second["carried_from"]["payment_id"] == r.json()["payment"]["id"] and second["valid_to"] == day(30).isoformat()
    assert third["carried_from"]["payment_id"] == second["id"] and third["valid_to"] == day(30).isoformat()
    assert len(history(two, two.second)) == 2 and len(history(two, two.third)) == 2          # the old ones stay, voided
    assert second["carried_out"] and not third["carried_out"]          # the middle link moved on, the last one is where it is


def test_a_carry_over_cannot_be_replaced_only_cancelled(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    r = owner(two).post(
        f"{API}/org/payments/{carry['id']}/replace",
        json={"amount": 5, "paid_on": day(0).isoformat(), "valid_to": day(40).isoformat()},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "CARRY_OVER_NOT_REPLACEABLE", "message": "Перенос нельзя исправить — исправьте исходную оплату"}
    assert history(two, two.second, include_voided=False)[0]["id"] == carry["id"]      # nothing changed
    assert history(two, two.group)[0]["id"] == original["id"] and history(two, two.group)[0]["voided_at"] is None
    # correcting the original is the way
    r = owner(two).post(
        f"{API}/org/payments/{original['id']}/replace",
        json={"amount": 5, "paid_on": day(-10).isoformat(), "valid_from": day(-10).isoformat(), "valid_to": day(30).isoformat()},
    )
    assert r.status_code == 201, r.text
    assert history(two, two.second, include_voided=False)[0]["valid_to"] == day(30).isoformat()


# ---- round trips ---------------------------------------------------------------------------------


def test_calendar_return_is_covered_again_only_through_the_new_carry_over(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (c1,) = history(two, two.second)
    transfer(two, two.group)                                          # back: the live carry-over on B moves again
    (c2,) = [h for h in history(two, two.group) if h["id"] != original["id"]]
    assert c2["carried_from"]["payment_id"] == c1["id"] and c2["carried_from"]["group_name"] == "Вторая"
    assert (c2["valid_from"], c2["valid_to"], c2["amount"]) == (day(0).isoformat(), day(20).isoformat(), 0)
    row = row_of(two, group=two.group)
    assert (row["status"], row["paid_until"]) == ("active", day(20).isoformat())
    # the old original is stamped, the carry-over on B is stamped in its turn: nothing is counted twice
    assert [h for h in history(two, two.group) if h["id"] == original["id"]][0]["carried_out"]["date"] == day(0).isoformat()
    assert history(two, two.second)[0]["carried_out"] == {"date": day(0).isoformat(), "group_name": "Группа"}
    closed = row_of(two, group=two.second)             # the carry-over moved on the day it was made
    assert (closed["status"], closed["paid_until"]) == ("ends_today", day(0).isoformat())
    # and once more forth: still exactly one live coverage on the destination
    transfer(two, two.second)
    live_b = history(two, two.second, include_voided=False)
    assert [h for h in live_b if not h["carried_out"]] and row_of(two, group=two.second)["paid_until"] == day(20).isoformat()
    assert len([h for h in live_b if not h["carried_out"]]) == 1


def test_without_a_new_carry_over_the_old_group_is_not_resurrected(two, monkeypatch):
    from src.auth import payment_status as status_module

    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    transfer(two, two.second)
    transfer(two, two.group, mode="add")                              # back to A without moving the payment
    row = row_of(two, group=two.group)
    assert (row["is_active"], row["status"], row["paid_until"]) == (True, "ends_today", day(0).isoformat())
    assert row_of(two, group=two.second)["status"] == "active"        # the live coverage stayed where it moved to
    monkeypatch.setattr(status_module, "business_today", lambda tz=None: day(2))     # two days later
    row = row_of(two, group=two.group)
    assert (row["status"], row["paid_until"], row["days_overdue"]) == ("expired", day(0).isoformat(), 2)


def test_lessons_return_leaves_original_minus_consumed_in_both_groups(two, monkeypatch):
    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    original = pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=6)["payment"]
    lesson_marks(two, 7, {})                                          # 2 lessons of A before the transfer
    lesson_marks(two, 6, {})
    with clock(monkeypatch, day(-4), 15):
        transfer(two, two.second)                                     # on day -4: 4 lessons travel
    (c1,) = history(two, two.second)
    assert (c1["lessons_count"], c1["valid_from"]) == (4, day(-4).isoformat())
    complete_in(two, two.second, 2)                                   # 1 lesson of B (day -2) spends the carried package
    row_b = row_of(two, group=two.second)
    assert (row_b["lessons_total"], row_b["lessons_used"], row_b["lessons_left"]) == (4, 1, 3)

    transfer(two, two.group)                                          # back today: 3 travel back
    (c2,) = [h for h in history(two, two.group) if h["carried_from"]]
    assert (c2["lessons_count"], c2["carried_from"]["payment_id"]) == (3, c1["id"])
    row_a = row_of(two, group=two.group)
    assert row_a["lessons_left"] == 6 - 2 - 1 == 3                    # original - consumed in A - consumed in B
    assert (row_a["lessons_total"], row_a["lessons_used"], row_a["period_mode"]) == (5, 2, "lessons")
    row_b = row_of(two, group=two.second)                              # B keeps only what it took
    assert (row_b["lessons_total"], row_b["lessons_used"], row_b["lessons_left"]) == (1, 1, 0)
    assert two.db.get(StudentPayment, original["id"]).lessons_count == 6    # the record itself is never rewritten


def test_lesson_before_the_transfer_stays_with_the_old_group_and_the_one_after_goes_to_the_new(two, monkeypatch):
    """A at 10:00, transfer at 15:00 the same day, B at 17:00: each lesson is paid by exactly one membership."""
    d = day(-2)
    add_slot(two, two.second, d, "evening-b", "17:00", "18:00")
    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=6)
    lesson_marks(two, 5, {})                                          # an earlier lesson of A
    lesson_marks(two, 2, {})                                          # day d, 10:00, the student is in A
    with clock(monkeypatch, d, 15):
        transfer(two, two.second)
    (c1,) = history(two, two.second)
    assert (c1["lessons_count"], c1["valid_from"]) == (4, d.isoformat())      # 6 - the two lessons of A
    assert hold(two, two.second, d, "evening-b") == [str(two.student.id)]      # day d, 17:00, the student is in B
    row_a, row_b = row_of(two, group=two.group), row_of(two, group=two.second)
    # the 10:00 lesson stays with the original (it started before the move): 2 taken, nothing left in A
    assert (row_a["lessons_total"], row_a["lessons_used"], row_a["lessons_left"]) == (2, 2, 0)
    # the 17:00 lesson is the carry-over's
    assert (row_b["lessons_total"], row_b["lessons_used"], row_b["lessons_left"]) == (4, 1, 3)
    assert row_a["lessons_used"] + row_b["lessons_used"] == 3                 # three lessons, each paid once


def test_calendar_the_transfer_day_is_covered_by_both_but_each_lesson_by_one(two, monkeypatch):
    d = day(-2)
    pay(two, paid_on=day(-12), valid_from=day(-12), valid_to=day(18))
    with clock(monkeypatch, d, 15):
        transfer(two, two.second)
    (c1,) = history(two, two.second)
    assert (c1["valid_from"], c1["valid_to"]) == (d.isoformat(), day(18).isoformat())
    (source,) = history(two, two.group)
    assert source["carried_out"]["date"] == d.isoformat()
    # the old group is paid up to and including the transfer day, the new one from it
    assert row_of(two, group=two.group)["paid_until"] == d.isoformat()
    assert row_of(two, group=two.second)["paid_until"] == day(18).isoformat()


def test_same_day_round_trip_splits_that_days_lessons_by_the_instant(two, monkeypatch):
    """A 10:00 (before the 15:00 transfer), back at 16:00, A 18:00 (after): the 18:00 lesson is the new carry-over's."""
    d = day(-2)
    add_slot(two, two.group, d, "evening-a", "18:00", "19:00")
    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=6)
    lesson_marks(two, 5, {})
    lesson_marks(two, 2, {})                                          # day d, 10:00
    with clock(monkeypatch, d, 15):
        transfer(two, two.second)
    with clock(monkeypatch, d, 16):
        transfer(two, two.group)                                      # back the same afternoon
    assert str(two.student.id) in hold(two, two.group, d, "evening-a")        # day d, 18:00, in A again
    (c2,) = [h for h in history(two, two.group) if h["carried_from"]]
    assert (c2["lessons_count"], c2["valid_from"]) == (4, d.isoformat())
    row = row_of(two, group=two.group)
    # the original keeps the two lessons that started before 15:00, the carry-over takes the 18:00 one
    assert (row["lessons_total"], row["lessons_used"], row["lessons_left"]) == (2 + 4, 3, 3)


def test_returning_after_the_carry_over_was_voided_carries_the_original_again(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    owner(two).post(f"{API}/org/payments/{carry['id']}/void", json={"reason": "Не нужен"})
    assert history(two, two.group)[0]["carried_out"] is None          # it covers its group again
    transfer(two, two.group)                                          # B has nothing live: nothing to carry
    assert [h["id"] for h in history(two, two.group, include_voided=False)] == [original["id"]]
    transfer(two, two.second)                                         # the original travels again
    assert len(history(two, two.second, include_voided=False)) == 1
    assert history(two, two.group)[0]["carried_out"]["date"] == day(0).isoformat()


def test_voiding_the_carry_over_gives_the_source_its_coverage_back(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    assert row_of(two, group=two.group)["status"] == "ends_today"
    (carry,) = history(two, two.second)
    r = owner(two).post(f"{API}/org/payments/{carry['id']}/void", json={"reason": "Не нужен"})
    assert r.status_code == 200, r.text
    (source,) = history(two, two.group)
    assert source["id"] == original["id"] and source["carried_out"] is None
    row = row_of(two, group=two.group)
    assert (row["status"], row["paid_until"]) == ("active", day(20).isoformat())
    assert row_of(two, group=two.second)["status"] == "none"


def test_a_second_live_carry_over_keeps_the_source_stamped(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    other = two.db.get(StudentPayment, carry["id"])
    twin = StudentGroup(student_id=two.student.id, group_id=two.third.id, is_active=False, joined_at=datetime.now())
    two.db.add(twin)
    two.db.flush()
    two.db.add(StudentPayment(
        org_id=two.org.id, membership_id=twin.id, student_id=two.student.id, group_id=two.third.id, amount=0,
        paid_on=other.paid_on, valid_from=other.valid_from, valid_to=other.valid_to, note="manual twin",
        carried_from_payment_id=other.carried_from_payment_id, created_by=two.owner.id))
    two.db.commit()
    owner(two).post(f"{API}/org/payments/{carry['id']}/void", json={"reason": "Не нужен"})
    assert history(two, two.group)[0]["carried_out"] is not None       # the twin still carries it
    assert history(two, two.group)[0]["id"] == original["id"]


def test_the_same_day_there_and_back_and_there_again_keeps_the_payment(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    transfer(two, two.second)
    transfer(two, two.group)
    transfer(two, two.second)                                         # identical figures, other source: allowed
    live = [h for h in history(two, two.second, include_voided=False) if not h["carried_out"]]
    assert len(live) == 1 and row_of(two, group=two.second)["paid_until"] == day(20).isoformat()
    assert row_of(two, group=two.group)["status"] == "ends_today"


def test_closed_group_does_not_change_the_summary_or_the_buckets(two):
    s0, s1, s2 = two.students
    pay(two, s0, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    before = summary(two)
    assert (before["tracked_total"], before["active"], before["none"]) == (3, 1, 2)
    transfer(two, two.second, s0)
    after = summary(two)
    assert (after["tracked_total"], after["active"], after["none"], after["due"]) == (3, 1, 2, 0)
    items = status_page(two, bucket="active")["items"]
    assert [(i["student_id"], i["group_id"]) for i in items] == [(str(s0.id), str(two.second.id))]
    assert all(i["is_active"] for i in status_page(two)["items"])
    # the closed membership is still readable on the card, and shows that the payment left
    closed = row_of(two, s0, two.group)
    assert closed["is_active"] is False and closed["status"] == "ends_today"


def test_a_student_in_two_groups_transferred_carries_from_both(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20), amount=1)
    transfer(two, two.second, mode="add")
    pay(two, group=two.second, paid_on=day(-5), valid_from=day(-5), valid_to=day(25), amount=2)
    result = transfer(two, two.third)                                 # closes both memberships
    assert len(carry_warnings(result)) == 2
    names = sorted(h["carried_from"]["group_name"] for h in history(two, two.third))
    assert names == ["Вторая", "Группа"]
    assert row_of(two, group=two.third)["paid_until"] == day(25).isoformat()


# ---- who sees what ---------------------------------------------------------------------------------


GENERIC = "Действующая оплата ученика перенесена в новую группу"


def allow_admin(w, flag=True):
    w.db.query(Organization).filter_by(id=w.org.id).update({"settings": {"payments": {"admin_can_manage": flag}}})
    w.db.commit()


def test_admin_without_the_right_gets_the_generic_message(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20), amount=SECRET)
    result = transfer(two, two.second, client=make_client(two))       # default identity = admin
    assert carry_warnings(result) == [{"code": "payment_carried_over", "message": GENERIC}]
    text = json.dumps(result, ensure_ascii=False)
    assert str(SECRET) not in text and day(20).strftime("%d.%m") not in text
    # the carry itself happened
    assert len(stored(two, membership_id=membership(two, two.second).id)) == 1


def test_admin_with_the_right_sees_the_dates(two):
    allow_admin(two)
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = transfer(two, two.second, client=make_client(two))
    assert carry_warnings(result)[0]["message"] == f"Оплата до {day(20).strftime('%d.%m')} перенесена в «Вторая»"


def test_admin_without_the_right_gets_the_generic_message_in_bulk_too(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    r = make_client(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(two.student.id)]})
    assert r.status_code == 200, r.text
    assert [w["message"] for w in r.json()["warnings"]] == [GENERIC]


def test_teacher_still_gets_403_and_nothing_moves(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    client = make_client(two, user=two.teacher)
    assert client.post(f"{GROUPS}/{two.second.id}/students/{two.student.id}").status_code == 403
    assert client.post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(two.student.id)]}).status_code == 403
    assert two.db.query(StudentGroup).filter_by(group_id=two.second.id).count() == 0
    assert membership(two, two.group).is_active is True


# ---- excuse deadline ------------------------------------------------------------------------------------


def deadline_of(w):
    r = make_client(w).get(f"{API}/org/attendance/excuses", params={
        "status": "all", "date_from": (business_today() - timedelta(days=10)).isoformat(),
        "date_to": business_today().isoformat()})
    assert r.status_code == 200, r.text
    (item,) = [i for i in r.json()["items"] if i["student"]["id"] == str(w.student.id)]
    return item["deadline"]


def excused_today_in(w, group, student):
    """Student marked excused in today's lesson of ``group`` (pending excuse)."""
    w.svc.start_group_attendance(
        str(w.org.id), str(group.id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(w.admin.id), is_org_admin=True,
    )
    w.svc.save_group_attendance(
        str(w.org.id), str(group.id),
        SaveAttendanceRequest(
            lessonDate=business_today(), lessonId=LIVE_SLOT, status="in_progress",
            records={str(student.id): "excused"}, excuseReasons={str(student.id): "Болел"},
        ),
        marked_by=str(w.admin.id), is_org_admin=True, is_org_manager=True,
    )


def deadline_in_group(w, student):
    r = make_client(w).get(f"{API}/org/attendance/excuses", params={
        "status": "all", "date_from": (business_today() - timedelta(days=5)).isoformat(),
        "date_to": business_today().isoformat()})
    assert r.status_code == 200, r.text
    (item,) = [i for i in r.json()["items"] if i["student"]["id"] == str(student.id)]
    return item["deadline"]


def test_excuse_deadline_counts_the_carry_over_as_a_covering_payment(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    transfer(two, two.second)
    excused_today_in(two, two.second, two.student)
    # the lesson is today, covered by the carry-over (valid_from = today .. day(20)): valid_to - 5 days
    assert deadline_in_group(two, two.student) == {
        "kind": "date", "date": day(15).isoformat(), "overdue": False, "days_left": 15}


def test_excuse_deadline_without_a_carry_over_falls_back_to_the_month_end(two):
    from src.auth.excuse_deadline import month_end

    transfer(two, two.second)                                          # nothing to carry
    excused_today_in(two, two.second, two.student)
    assert deadline_in_group(two, two.student)["date"] == month_end(business_today()).isoformat()


def test_excuse_deadline_follows_a_carried_lessons_package(two):
    set_settings(two, period_mode="lessons", lessons_per_payment=5, excuse_deadline_lessons=1)
    pay(two, paid_on=day(-8), valid_from=day(-8), lessons_count=5)
    lesson_marks(two, 3, {})
    lesson_marks(two, 2, {})
    transfer(two, two.second)                                          # 3 lessons are left in the carried package
    excused_today_in(two, two.second, two.student)
    d = deadline_in_group(two, two.student)
    # today's unfinished lesson would take the 1st of the 3 carried lessons: 2 left > N=1
    assert (d["kind"], d["lessons_left"], d["overdue"]) == ("lessons", 2, False)


def test_excuse_deadline_in_the_old_group_uses_the_stamp(two):
    from tests.pg_world import excused_lesson

    excused_lesson(two, offset_days=3, complete=True)                  # pending excuse for a lesson of A, 3 days ago
    pay(two, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    assert deadline_of(two) == {"kind": "date", "date": day(15).isoformat(), "overdue": False, "days_left": 15}
    transfer(two, two.second)
    # the payment left with the student: in A it now ends on the transfer day, so that is the end the deadline follows
    assert deadline_of(two) == {"kind": "date", "date": day(0).isoformat(), "overdue": False, "days_left": 0}


def test_excuse_deadline_in_the_old_group_follows_what_the_package_took(two):
    from tests.pg_world import excused_lesson

    set_settings(two, period_mode="lessons", lessons_per_payment=5, excuse_deadline_lessons=1)
    pay(two, paid_on=day(-8), valid_from=day(-8), lessons_count=5)
    excused_lesson(two, offset_days=3, complete=True)                  # lesson of A, 3 days ago, taken by the package
    lesson_marks(two, 2, {})
    assert deadline_of(two)["kind"] == "lessons" and deadline_of(two)["lessons_left"] == 3
    transfer(two, two.second)                                          # the 3 unspent lessons travel
    after = deadline_of(two)                                           # the package is the student's live one now
    assert (after["kind"], after["lessons_left"], after["overdue"]) == ("lessons", 3, False)


def test_excuse_deadline_of_a_moved_package_follows_the_live_remainder(two, monkeypatch):
    from tests.pg_world import excused_lesson

    set_settings(two, period_mode="lessons", lessons_per_payment=5, excuse_deadline_lessons=1)
    pay(two, paid_on=day(-9), valid_from=day(-9), lessons_count=5)
    excused_lesson(two, offset_days=6, complete=True)                  # the excused lesson of A (counts while pending)
    lesson_marks(two, 5, {})
    with clock(monkeypatch, day(-4), 12):
        transfer(two, two.second)                                      # 3 lessons left travel to B
    complete_in(two, two.second, 2)                                    # B spends one of them
    assert deadline_of(two) == {"kind": "lessons", "lessons_left": 2, "urgent": False, "due_now": False, "overdue": False}
    complete_in(two, two.second, 1)                                    # and another: 1 left = the threshold
    last = deadline_of(two)
    assert (last["lessons_left"], last["due_now"], last["overdue"]) == (1, True, False)


# ---- conservation: the package that travels is always the original minus what was consumed -----------


def confirm(w, excuse):
    r = make_client(w).post(f"{API}/org/attendance/excuses/{excuse.id}/review", json={"decision": "confirmed"})
    assert r.status_code == 200, r.text


def test_confirming_a_pre_transfer_excuse_after_the_transfer_gives_the_lesson_back(two):
    from tests.pg_world import excused_lesson
    from tests.test_attendance_excuses import excuse_of

    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=6)
    excused_lesson(two, offset_days=4, complete=True)                  # pending excuse: still spends a lesson
    lesson_marks(two, 3, {})
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    assert carry["lessons_count"] == 4                                  # the remainder at transfer time (informational)
    row_b = row_of(two, group=two.second)
    assert (row_b["lessons_total"], row_b["lessons_left"]) == (4, 4)
    confirm(two, excuse_of(two))                                        # the certificate arrives later
    row_a, row_b = row_of(two, group=two.group), row_of(two, group=two.second)
    assert (row_a["lessons_total"], row_a["lessons_used"]) == (1, 1)    # A keeps one consumed lesson
    assert (row_b["lessons_total"], row_b["lessons_left"]) == (5, 5)    # the lesson went to the live package
    assert row_a["lessons_used"] + row_b["lessons_left"] == 6           # conserved
    assert history(two, two.second)[0]["lessons_count"] == 4            # the stored number is never the truth


def test_reconciling_a_pre_transfer_lesson_after_the_transfer_takes_a_lesson_from_the_live_package(two):
    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, paid_on=day(-12), valid_from=day(-12), lessons_count=6)
    lesson_marks(two, 5, {})                                            # one lesson of A is recorded
    transfer(two, two.second)                                           # the lesson of day -3 was never opened
    assert row_of(two, group=two.second)["lessons_left"] == 5
    d = day(-3)
    hold(two, two.group, d, f"s{d.isoweekday()}")                       # ... and is reconciled afterwards
    row_a, row_b = row_of(two, group=two.group), row_of(two, group=two.second)
    assert (row_a["lessons_total"], row_a["lessons_used"]) == (2, 2)
    assert (row_b["lessons_total"], row_b["lessons_left"]) == (4, 4)    # not 6 + 5: the package stays 6 in all
    assert row_a["lessons_used"] + row_b["lessons_left"] == 6


def test_a_package_through_a_b_a_b_is_conserved_and_follows_late_changes(two, monkeypatch):
    from tests.pg_world import excused_lesson
    from tests.test_attendance_excuses import excuse_of

    set_settings(two, period_mode="lessons", lessons_per_payment=6)
    pay(two, paid_on=day(-20), valid_from=day(-20), lessons_count=6)
    excused_lesson(two, offset_days=9, complete=True)                   # A: two lessons, one of them with a pending excuse
    lesson_marks(two, 8, {})
    with clock(monkeypatch, day(-7), 12):
        transfer(two, two.second)                                       # A -> B
    complete_in(two, two.second, 5)                                     # B: one lesson
    with clock(monkeypatch, day(-4), 12):
        transfer(two, two.group)                                        # B -> A
    hold(two, two.group, day(-2), f"s{day(-2).isoweekday()}")           # A again: one lesson
    with clock(monkeypatch, day(-1), 12):
        transfer(two, two.second)                                       # A -> B

    def totals():
        a, b = row_of(two, group=two.group), row_of(two, group=two.second)
        return a["lessons_used"] + b["lessons_used"], b["lessons_left"], a["lessons_left"]

    used, left_b, left_a = totals()
    assert (used, left_b, left_a) == (4, 2, 0) and used + left_b == 6    # 2 + 1 + 1 consumed, 2 travelling
    confirm(two, excuse_of(two))                                         # the first lesson turns out to be excused
    used, left_b, left_a = totals()
    assert (used, left_b, left_a) == (3, 3, 0) and used + left_b == 6
    chain = [h for h in history(two, two.second, include_voided=False) if not h["carried_out"]]
    assert len(chain) == 1 and chain[0]["lessons_count"] == 2            # stored at its transfer: informational only


# ---- locks: one order everywhere, a concurrent void is waited for ---------------------------------------


@pytest.fixture
def engine():
    from sqlalchemy import create_engine

    from tests.pg_world import DB_URL

    engine = create_engine(DB_URL)
    yield engine
    engine.dispose()


def run_in_thread(engine, fn):
    import threading

    from sqlalchemy.orm import Session

    outcome = {}

    def target():
        with Session(engine) as session:
            try:
                outcome["value"] = fn(session)
            except BaseException as exc:  # noqa: BLE001  (reported to the test)
                outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def wait_until_blocked(engine, seconds=10):
    import time

    from sqlalchemy import text

    deadline = time.time() + seconds
    while time.time() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )).scalar_one()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the request never waited for a lock")


def row_is_free(engine, table, row_id):
    """True if nobody holds the row lock (checked with NOWAIT from a third connection)."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with engine.connect() as conn:
        try:
            conn.execute(text(f"SELECT id FROM {table} WHERE id = :i FOR UPDATE NOWAIT"), {"i": row_id})
            return True
        except OperationalError:
            return False


def test_transfer_waits_for_a_payment_being_voided_and_does_not_carry_it(two, engine):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth.service import AuthService

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    holder = Session(engine)
    holder.execute(text(  # a void in flight: the row is locked and already marked, not yet committed
        "UPDATE student_payments SET voided_at = now(), voided_by = :u, void_reason = 'ошибка' WHERE id = :p"),
        {"u": two.owner.id, "p": original["id"]})
    thread, outcome = run_in_thread(engine, lambda session: AuthService(session).add_student_to_group(
        str(two.org.id), str(two.second.id), str(two.student.id), actor_id=str(two.owner.id)))
    wait_until_blocked(engine)
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"]["status"] == "added" and outcome["value"]["warnings"] == []
    two.db.expire_all()
    assert stored(two, membership_id=membership(two, two.second).id) == []        # nothing carried
    assert two.db.get(StudentPayment, original["id"]).carried_out_at is None      # and nothing stamped


def test_replace_takes_the_membership_before_the_payment(two, engine):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth.payments_schemas import PaymentReplace
    from src.auth.payments_service import PaymentService

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    m = membership(two, two.group)
    holder = Session(engine)                                  # a transfer in flight holds the membership
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR UPDATE"), {"m": m.id})
    body = PaymentReplace(amount=1, paid_on=day(-10), valid_from=day(-10), valid_to=day(25))
    thread, outcome = run_in_thread(engine, lambda session: PaymentService(session).replace_payment(
        str(two.org.id), original["id"], str(two.owner.id), body))
    wait_until_blocked(engine)
    assert row_is_free(engine, "student_payments", original["id"])       # waiting for the membership, payment untouched
    holder.rollback()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"].payment.valid_to == day(25)


def test_void_of_a_carry_over_takes_its_source_before_itself(two, engine):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth.payments_service import PaymentService

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    holder = Session(engine)                                  # somebody holds the source payment
    holder.execute(text("SELECT id FROM student_payments WHERE id = :p FOR UPDATE"), {"p": original["id"]})
    thread, outcome = run_in_thread(engine, lambda session: PaymentService(session).void_payment(
        str(two.org.id), carry["id"], str(two.owner.id), "Не нужен"))
    wait_until_blocked(engine)
    assert row_is_free(engine, "student_payments", carry["id"])          # the carry-over is not locked before its source
    holder.rollback()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert history(two, two.group)[0]["carried_out"] is None             # the source covers its group again


def deadlock():
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        pgcode = "40P01"

    return OperationalError("UPDATE ...", {}, Orig("deadlock detected"))


def test_a_deadlock_is_a_409_to_repeat_never_a_500(two, monkeypatch):
    from src.auth.payments_service import PaymentService
    from src.auth.service import AuthService

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]

    def boom(*args, **kwargs):
        raise deadlock()

    message = "Операция совпала с другой правкой. Повторите."
    monkeypatch.setattr(PaymentService, "_probe_payment", boom)
    for url, body in ((f"{API}/org/payments/{original['id']}/void", {"reason": "проверка"}),
                      (f"{API}/org/payments/{original['id']}/replace", {"amount": 1, "paid_on": day(0).isoformat(), "valid_to": day(9).isoformat()})):
        r = owner(two).post(url, json=body)
        assert r.status_code == 409 and r.json()["detail"] == message, (url, r.text)
    monkeypatch.undo()
    monkeypatch.setattr(AuthService, "add_student_to_group", boom)
    monkeypatch.setattr(AuthService, "bulk_add_students", boom)
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/{two.student.id}")
    assert r.status_code == 409 and r.json()["detail"] == message, r.text
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(two.student.id)]})
    assert r.status_code == 409 and r.json()["detail"] == message, r.text


def test_other_database_errors_are_not_disguised_as_a_conflict(two, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from src.auth.payments_service import PaymentService

    def broken(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(PaymentService, "_probe_payment", broken)
    with pytest.raises(OperationalError):
        owner(two).post(f"{API}/org/payments/{uuid4()}/void", json={"reason": "проверка"})


# ---- membership locks are FOR NO KEY UPDATE -------------------------------------------------------------


def test_every_membership_lock_is_for_no_key_update(two):
    from sqlalchemy import event

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    engine = two.db.get_bind()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        set_fee(two, two.group, 300000)                                       # payments: _membership(lock=True)
        owner(two).post(f"{API}/org/payments/{original['id']}/void", json={"reason": "проверка"})
        pay(two, group=two.group, paid_on=day(-1), valid_to=day(30), amount=3)
        transfer(two, two.second)                                              # transfer: target + closed memberships
        assert owner(two).delete(f"{GROUPS}/{two.second.id}/students/{two.student.id}").status_code == 200   # remove
    finally:
        event.remove(engine, "before_cursor_execute", record)
    locking = [s for s in statements if "FROM student_groups" in s and " FOR " in s]
    assert len(locking) >= 5, locking
    assert all("FOR NO KEY UPDATE" in s for s in locking), [s for s in locking if "FOR NO KEY UPDATE" not in s]


def test_a_carry_over_insert_is_not_blocked_by_a_membership_lock(two, engine):
    """The cascade of a replace inserts a carry-over into a membership a transfer may hold (FK = KEY SHARE)."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth.payments_schemas import PaymentReplace
    from src.auth.payments_service import PaymentService

    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    target = membership(two, two.second).id

    def replace(valid_to):
        body = PaymentReplace(amount=1, paid_on=day(-10), valid_from=day(-10), valid_to=valid_to)
        current = [h for h in history(two, two.group, include_voided=False)][0]["id"]
        return run_in_thread(engine, lambda session: PaymentService(session).replace_payment(
            str(two.org.id), current, str(two.owner.id), body))

    holder = Session(engine)                                  # a transfer in flight: FOR NO KEY UPDATE on the new membership
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR NO KEY UPDATE"), {"m": target})
    thread, outcome = replace(day(25))
    thread.join(timeout=10)
    assert not thread.is_alive(), "the carry-over insert waited for a membership lock that only guards its editors"
    assert "error" not in outcome, outcome.get("error")
    holder.rollback()
    holder.close()
    assert history(two, two.second, include_voided=False)[0]["valid_to"] == day(25).isoformat()

    holder = Session(engine)                                  # control: a plain FOR UPDATE does block that insert
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR UPDATE"), {"m": target})
    thread, outcome = replace(day(26))
    wait_until_blocked(engine)
    holder.rollback()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert history(two, two.second, include_voided=False)[0]["valid_to"] == day(26).isoformat()


def test_removing_a_student_maps_a_deadlock_to_409(two, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from src.auth.service import AuthService

    def boom(*args, **kwargs):
        raise deadlock()

    monkeypatch.setattr(AuthService, "remove_student_from_group", boom)
    r = owner(two).delete(f"{GROUPS}/{two.group.id}/students/{two.student.id}")
    assert r.status_code == 409 and r.json()["detail"] == "Операция совпала с другой правкой. Повторите.", r.text

    def broken(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(AuthService, "remove_student_from_group", broken)
    with pytest.raises(OperationalError):
        owner(two).delete(f"{GROUPS}/{two.group.id}/students/{two.student.id}")
