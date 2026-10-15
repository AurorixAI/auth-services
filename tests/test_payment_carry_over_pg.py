"""N13 P2.1: the payment follows the student on a transfer (PostgreSQL)."""
import json
from datetime import datetime, timedelta

import pytest

from src.auth.schemas import SaveAttendanceRequest, StartAttendanceRequest
from src.models.auth import Organization, Student, StudentGroup, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, make_client, make_group, make_student, needs_pg, pg_session, world,
)
from tests.test_payments_service_pg import (  # noqa: F401
    API, day, lesson_marks, mid, owner, pay, row_of, set_settings,
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
    # the original is untouched and still on the closed group
    (kept,) = history(two, two.group)
    assert kept["id"] == original["id"] and kept["amount"] == SECRET and kept["voided_at"] is None
    assert kept["carried_from"] is None


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


def test_replacing_a_carry_over_directly_is_an_ordinary_replace(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    r = owner(two).post(
        f"{API}/org/payments/{carry['id']}/replace",
        json={"amount": 5, "paid_on": day(0).isoformat(), "valid_to": day(40).isoformat()},
    )
    assert r.status_code == 201, r.text
    assert r.json()["payment"]["carried_from"] is None
    assert history(two, two.group)[0]["id"] == original["id"] and history(two, two.group)[0]["voided_at"] is None


# ---- round trips ---------------------------------------------------------------------------------


def test_returning_to_the_old_group_does_not_double_the_coverage(two):
    original = pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))["payment"]
    transfer(two, two.second)
    transfer(two, two.group)                                          # back: A is reactivated with its own payment
    live_a = history(two, two.group, include_voided=False)
    assert [p["id"] for p in live_a] == [original["id"]]
    assert row_of(two, group=two.group)["paid_until"] == day(20).isoformat()
    transfer(two, two.second)                                         # and forth again: the carry-over is still live there
    assert len(history(two, two.second, include_voided=False)) == 1


def test_returning_after_the_carry_over_was_voided_carries_again(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    transfer(two, two.second)
    (carry,) = history(two, two.second)
    owner(two).post(f"{API}/org/payments/{carry['id']}/void", json={"reason": "Не нужен"})
    transfer(two, two.group)
    transfer(two, two.second)
    assert len(history(two, two.second, include_voided=False)) == 1


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
