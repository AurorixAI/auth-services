"""Money-2, the round after the independent review of the server (R3, 16.11): П1-П4 and the small things (PostgreSQL).  The numbers of every money test are worked by hand in its comment."""
from datetime import date, timedelta

import pytest

from src.models.auth import GroupAudit, StudentGroup
from tests.money1b_helpers import add, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_transfer_a_pg import groups3, joined, nov_charge, snap
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

FRIDAY = date(2026, 11, 27)
TUE_THU = [slot("tu", 2, "2026-01-01"), slot("th", 4, "2026-01-01")]          # no lesson on Friday: the next one is Tuesday 01.12


# ============================================================================================================ П1: the return on the day of leaving does not move «Оплата с»


def leave(w, group, student):
    r = owner(w).post(f"{API}/org/groups/{group.id}/students/{student.id}/leave", json={})
    assert r.status_code == 200, r.text


def periods_billing_from(w, student, group):
    w.db.expire_all()
    return sorted((p.joined_at, p.billing_from) for p in membership(w, student, group).periods)


@pytest.mark.parametrize("settings", [
    {},                                                                               # «Общий месяц»: calendar months from the 1st
    {"billing_anchor": "payment_date"},                                               # the personal period
    {"period_mode": "lessons", "lessons_per_payment": 8},                             # a package centre
], ids=["shared_month", "personal_period", "package"])
def test_p1_leaving_and_coming_back_on_friday_in_a_tuesday_thursday_group_gives_every_number_back(world, fixed_today, settings):
    # a group of Tuesdays and Thursdays, price 300 000 a month, the student since Mon 02.11, paid 300 000; he leaves and comes back on Friday 27.11 (no lesson that day: the next one is Tue 01.12).
    # Before the fix the return opened a NEW period with «Оплата с» = 01.12: 27-30.11 became a pause and the «paid until» moved.  Now the stay goes on as if he had not left: every number as before.
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    if settings:
        settings_block(world, **settings)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    leave(world, a, s0)
    out = add(world, s0, group=a, mode="add")
    assert out["leave_undo"]["undone"] is True and out["billing"] is None            # no new «Оплата с»: nothing was started
    assert snap(world, s0, a) == before
    assert all(billing_from is None for _, billing_from in periods_billing_from(world, s0, a))
    if not settings:
        assert nov_charge(world, s0, a) == 300_000                                   # a partial month goes by lessons: Tue 3, 10, 17, 24 and Thu 5, 12, 19, 26 = 8 of 8 lessons of November are in the stay -> the whole 300 000


def test_p1_an_explicit_pay_from_or_opening_balance_with_a_cancelled_leaving_is_refused_and_nothing_is_written(world, fixed_today):
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000)
    leave(world, a, s0)
    gone = snap(world, s0, a)
    for body in ({"pay_from": "2026-12-01"}, {"opening_balance": {"debt_amount": 100_000, "debt_on": "2026-11-10"}}):
        r = add(world, s0, group=a, mode="add", expect=422, **body)
        assert r["detail"]["code"] == "LEAVE_UNDO_NO_START" and "уход отменяется" in r["detail"]["message"]
        assert snap(world, s0, a) == gone and membership(world, s0, a).is_active is False


def test_p1_the_return_after_the_end_of_the_paid_period_and_the_next_day_stay_ordinary(world, fixed_today):
    # the paid period ended earlier (paid 150 000 on 05.11 = 15 days of 300 000/30 -> covered through 19.11); on Friday 27.11 he leaves and comes back the same day: the leaving is cancelled,
    # the stay is not touched, the debt of 20.11-26.11 stays what it was
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=150_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    leave(world, a, s0)
    assert add(world, s0, group=a, mode="add")["leave_undo"]["undone"] is True
    assert snap(world, s0, a) == before


def test_p1_restoring_a_student_archived_today_into_the_same_group_is_the_same_cancel(world, fixed_today):
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    assert owner(world).post(f"{API}/org/students/{s0.id}/archive", json={}).status_code == 200
    r = owner(world).post(f"{API}/org/students/{s0.id}/restore", json={"group_ids": [str(a.id)]})
    assert r.status_code == 200 and not r.json()["skipped"], r.text
    assert snap(world, s0, a) == before
    assert all(billing_from is None for _, billing_from in periods_billing_from(world, s0, a))


# ============================================================================================================ П2: a transfer always names its source


def test_p2_the_second_administrator_with_an_old_window_and_no_source_is_refused_and_nothing_is_written(world, fixed_today):
    # admin 1 moved A -> B; admin 2 (a window opened earlier) sends A -> C without a source: the student has ONE group now (B), but a transfer without a source is never guessed
    from src.models.auth import MembershipTransfer

    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    add(world, s0, group=b, mode="transfer", from_group_id=str(a.id))
    r = add(world, s0, group=c, mode="transfer", from_group_id=None, expect=422)
    assert r["detail"]["code"] == "TRANSFER_SOURCE_REQUIRED" and [g["group_id"] for g in r["detail"]["groups"]] == [str(b.id)]
    assert membership(world, s0, b).is_active is True and world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=c.id).count() == 0
    assert world.db.query(MembershipTransfer).filter_by(student_id=s0.id).count() == 1


def test_p2_a_student_of_one_group_needs_the_source_too(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students                                                    # all three are in A
    r = add(world, s0, group=b, mode="transfer", from_group_id=None, expect=422)
    assert r["detail"]["code"] == "TRANSFER_SOURCE_REQUIRED" and membership(world, s0, a).is_active
    add(world, s0, group=b, mode="transfer", from_group_id=str(a.id))
    assert membership(world, s0, a).is_active is False


def test_p2_a_source_the_student_never_had_says_so_and_a_source_he_just_lost_says_that(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    r = add(world, s0, group=b, mode="transfer", from_group_id=str(c.id), expect=422)                       # never in C
    assert r["detail"]["code"] == "TRANSFER_SOURCE_NOT_MEMBER" and r["detail"]["message"] == "Ученик не состоит в группе «Гамма»"
    add(world, s0, group=b, mode="transfer", from_group_id=str(a.id))
    r = add(world, s0, group=c, mode="transfer", from_group_id=str(a.id), expect=409)                       # was in A, moved a moment ago
    assert r["detail"]["code"] == "TRANSFER_SOURCE_GONE"


def test_p2_a_bulk_transfer_takes_the_source_of_each_student_or_a_common_one_and_a_stranger_of_the_common_source_has_his_own_text(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    add(world, s2, group=c, mode="transfer", from_group_id=str(a.id))                # s2 is in C only (was in A); s0, s1 are in A
    url = f"{API}/org/groups/{b.id}/students/bulk"
    r = owner(world).post(url, params={"mode": "transfer"}, json={"student_ids": [str(s0.id), str(s1.id)]})        # no source at all: every student has his line
    assert r.status_code == 200 and r.json()["added"] == 0 and {d["code"] for d in r.json()["skipped_details"]} == {"TRANSFER_SOURCE_REQUIRED"}
    r = owner(world).post(url, params={"mode": "transfer"}, json={"student_ids": [str(s0.id), str(s1.id), str(s2.id)], "from_group_id": str(a.id), "sources": {str(s2.id): str(c.id)}})
    out = r.json()
    assert r.status_code == 200 and out["added"] == 3 and out["skipped"] == 0                                    # s2 has his own source (C), the others the common one (A)
    assert membership(world, s2, c).is_active is False and membership(world, s0, a).is_active is False
    r = owner(world).post(f"{API}/org/groups/{c.id}/students/bulk", params={"mode": "transfer"}, json={"student_ids": [str(s0.id)], "from_group_id": str(a.id)})
    (d,) = r.json()["skipped_details"]                                                                         # s0 left A a moment ago (moved to B)
    assert d["code"] == "TRANSFER_SOURCE_GONE"
    r = owner(world).post(f"{API}/org/groups/{a.id}/students/bulk", params={"mode": "transfer"}, json={"student_ids": [str(s0.id)], "from_group_id": str(c.id)})
    (d,) = r.json()["skipped_details"]
    assert d["code"] == "TRANSFER_SOURCE_NOT_MEMBER" and d["reason"] == "Ученик не состоит в группе «Гамма»"


def test_p2_an_import_row_that_meets_the_missing_source_is_its_own_line_and_does_not_stop_the_file(world, fixed_today, monkeypatch):
    # the student joined A between the check of the import and the enrolment (the check is blinded): the row says IN_OTHER_GROUP, the file goes on
    from src.auth import import_ops
    from src.auth.service import AuthService

    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    monkeypatch.setattr(import_ops, "_live_groups_of", lambda *args, **kw: {})
    item = {"existing": s0, "row": 2, "group_name": b.name, "first": "x", "last": "y", "class_level": None, "phone": None, "comment": None}
    out = import_ops._enrol(world.db, AuthService(world.db), world.org.id, world.owner.id, item, b.id)
    assert out[:2] == ("skipped", "IN_OTHER_GROUP")
    assert membership(world, s0, a).is_active is True


# ============================================================================================================ П3: the ceiling of a student who left is not bypassed by a restoration or a correction back


def left_with_debt_100(world, fixed_today, index=0):
    """A student of A since 02.11 (300 000 a month), paid 110 000 on 02.11, leaves on 22.11: the stay 02..22 = 21 days x 300 000 / 30 = 210 000 charged (as in T7).
    210 000 - 110 000 = a debt of 100 000.  Returns ``(a, student)``."""
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22)) if index == 0 else (world.group, None, None)
    s0 = world.students[index]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=110_000, paid_on=date(2026, 11, 2))
    leave(world, a, s0)
    assert account(world, membership(world, s0, a).id).debt == 100_000
    return a, s0


def account(w, m):
    from tests.money1b_helpers import account as acc

    return acc(w, m)


def live_sum(w, group, student):
    w.db.expire_all()
    from src.models.auth import StudentPayment

    return sum(int(p.amount) for p in w.db.query(StudentPayment).filter_by(membership_id=membership(w, student, group).id, voided_at=None))


def void_it(w, payment, client=None, expect=200):
    r = (client or owner(w)).post(f"{API}/org/payments/{payment['id']}/void", json={"reason": "Внесли по ошибке"})
    assert r.status_code == expect, r.text
    return r.json()


def restore_it(w, payment, expect=201):
    r = owner(w).post(f"{API}/org/payments/{payment['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == expect, r.text
    return r.json()


def replace_it(w, payment, amount, client=None, expect=201, **kw):
    r = (client or owner(w)).post(f"{API}/org/payments/{payment['id']}/replace", json={"amount": amount, "paid_on": payment["paid_on"], "method": "cash", "reason": "Исправление суммы", **kw})
    assert r.status_code == expect, r.text
    return r.json()


def test_p3_restoring_a_payment_after_another_one_was_taken_in_its_place_is_refused(world, fixed_today):
    # debt 100 000; P1 = 100 000 (debt 0); P1 cancelled (debt 100 000 again); P2 = 100 000 is taken (debt 0).  Restoring P1 would make 110 000 + 100 000 + 100 000 = 310 000 paid for 210 000 charged: an advance 100 000 to return.
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    void_it(world, p1)
    pay(world, s0, a, amount=100_000)
    d = restore_it(world, p1, expect=409)["detail"]
    assert d["code"] == "PAYMENT_RESTORE_SUPERSEDED" and d["message"] == "После отмены в эту группу уже внесена другая оплата — восстановить нельзя: исправьте её сумму или внесите разницу"
    assert live_sum(world, a, s0) == 210_000 and account(world, membership(world, s0, a).id).advance == 0
    assert owner(world).post(f"{API}/org/payments/{p1['id']}/restore", params={"dry_run": "true"}, json={"reason": "Отменили по ошибке"}).status_code == 409      # the window says it too


def test_p3_a_correction_back_to_the_biggest_sum_is_refused_when_another_payment_came_in_between(world, fixed_today):
    # debt 100 000; P1 = 100 000 -> corrected to 10 000 (debt 90 000) -> P2 = 90 000 (debt 0) -> P1 back to 100 000: the old ceiling (the biggest of the chain = 100 000) let 110 000 + 100 000 + 90 000 = 300 000 in.
    # Now the ceiling = max(debt without this payment = 100 000 - 90 000 = 10 000, its current sum 10 000) = 10 000.
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    cut = replace_it(world, p1, 10_000)["payment"]
    pay(world, s0, a, amount=90_000)
    d = replace_it(world, cut, 100_000, expect=422)["detail"]
    assert d["code"] == "PAYMENT_EXCEEDS_DEBT" and d["debt"] == 10_000
    assert live_sum(world, a, s0) == 110_000 + 10_000 + 90_000 and account(world, membership(world, s0, a).id).advance == 0
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s0, a).id}/payments/preview", params={"amount": 100_000, "replace_payment_id": cut["id"]})
    assert r.status_code == 200 and r.json()["suggested_amount"] == 10_000          # the hint of the window is the same ceiling


def test_p3_the_honest_cases_stay(world, fixed_today):
    # (1) nothing between: the cancelled P1 = 100 000 comes back: 110 000 + 100 000 = 210 000 = the charge, no advance
    a, s0 = left_with_debt_100(world, fixed_today)
    p1 = pay(world, s0, a, amount=100_000)["payment"]
    void_it(world, p1)
    restore_it(world, p1)
    assert live_sum(world, a, s0) == 210_000 and account(world, membership(world, s0, a).id).advance == 0
    # (2) a payment taken after the cancellation and cancelled itself is not "another live payment": P1 may come back
    a, s1 = left_with_debt_100(world, fixed_today, index=1)
    q1 = pay(world, s1, a, amount=100_000)["payment"]
    void_it(world, q1)
    q2 = pay(world, s1, a, amount=100_000)["payment"]
    void_it(world, q2)
    restore_it(world, q1)
    assert live_sum(world, a, s1) == 210_000
    # (3) 100 000 -> 10 000 -> 100 000 with nothing between: put back (the sums of the chain)
    s2 = world.students[2]
    joined(world, s2, a, date(2026, 11, 2))
    pay(world, s2, a, amount=110_000, paid_on=date(2026, 11, 2))
    leave(world, a, s2)
    r1 = pay(world, s2, a, amount=100_000)["payment"]
    cut = replace_it(world, r1, 10_000)["payment"]
    replace_it(world, cut, 100_000)
    assert live_sum(world, a, s2) == 210_000


# ============================================================================================================ П4: an administrator changes no money of a record older than 7 days


def admin_client(world):
    from tests.test_money2_wave_b_pg import _admin_with_the_right

    return _admin_with_the_right(world)


def world_p4(world, fixed_today):
    """Today 20.11.2026.  Payments of S0: old = 02.11 (18 days) 300 000; edge8 = 12.11 (8 days) 10 000; edge7 = 13.11 (7 days) 10 000, all through the owner."""
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    old = pay(world, s0, a, amount=300_000, paid_on=date(2026, 11, 2))["payment"]
    e8 = pay(world, s0, a, amount=10_000, paid_on=date(2026, 11, 12))["payment"]
    e7 = pay(world, s0, a, amount=10_000, paid_on=date(2026, 11, 13))["payment"]
    return a, s0, old, e8, e7


def test_p4_an_administrator_cannot_change_the_sum_cancel_or_move_the_date_of_a_payment_older_than_seven_days_only_the_method(world, fixed_today):
    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    admin = admin_client(world)
    for amount in (1_000_000, 100):                                                   # up and down: both refused
        d = replace_it(world, old, amount, client=admin, expect=403)["detail"]
        assert d["code"] == "BACKDATE_OWNER_ONLY" and d["message"] == "Запись старше 7 дней меняет владелец"
    assert replace_it(world, old, 300_000, client=admin, expect=403, paid_on="2026-11-13")["detail"]["code"] == "BACKDATE_OWNER_ONLY"      # a new date of an old record
    assert void_it(world, old, client=admin, expect=403)["detail"]["code"] == "BACKDATE_OWNER_ONLY"
    assert admin.post(f"{API}/org/payments/{old['id']}/replace", params={"dry_run": "true"}, json={"amount": 1_000_000, "paid_on": old["paid_on"], "method": "cash", "reason": "Проба"}).status_code == 403
    url = f"{API}/org/memberships/{membership(world, s0, a).id}/payments/preview"
    assert admin.get(url, params={"amount": 1_000_000, "paid_on": old["paid_on"], "replace_payment_id": old["id"]}).status_code == 403
    assert admin.get(url, params={"amount": 300_000, "paid_on": old["paid_on"], "replace_payment_id": old["id"]}).status_code == 200
    assert live_sum(world, a, s0) == 320_000                                           # nothing changed
    r = admin.post(f"{API}/org/payments/{old['id']}/replace", json={"amount": 300_000, "paid_on": old["paid_on"], "method": "card", "reason": "Способ"})
    assert r.status_code == 201, r.text                                                # the method (and the note) of an old record: allowed
    assert live_sum(world, a, s0) == 320_000


def test_p4_the_border_seven_and_eight_days_and_the_owner_is_free(world, fixed_today):
    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    admin = admin_client(world)
    assert replace_it(world, e8, 20_000, client=admin, expect=403)["detail"]["code"] == "BACKDATE_OWNER_ONLY"                 # 8 days
    assert void_it(world, e8, client=admin, expect=403)["detail"]["code"] == "BACKDATE_OWNER_ONLY"
    replace_it(world, e7, 20_000, client=admin)                                                                                 # 7 days: as before
    replace_it(world, old, 250_000)                                                                                             # the owner: any record, any sum
    assert live_sum(world, a, s0) == 250_000 + 10_000 + 20_000


def test_p4_the_cancellation_of_an_old_refund_and_an_old_forgiveness_is_the_owners_and_the_restoration_stays_free(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    admin = admin_client(world)
    m1, m2 = membership(world, s1, a), membership(world, s2, a)
    pay(world, s2, a, amount=5_000_000, paid_on=date(2026, 11, 2))
    refunds, writeoffs = [], []
    for day in ("2026-11-12", "2026-11-13"):                                           # 8 and 7 days back (the owner enters them)
        r = owner(world).post(f"{API}/org/memberships/{m2.id}/refunds", json={"amount": 1_000, "method": "cash", "reason": "Возврат", "effective_on": day})
        assert r.status_code == 201, r.text
        refunds.append(r.json()["refund"]["id"])
        w = owner(world).post(f"{API}/org/memberships/{m1.id}/writeoffs", json={"amount": 1_000, "reason": "Договорённость", "effective_on": day})
        assert w.status_code == 201, w.text
        writeoffs.append(w.json()["writeoff"]["id"])
    for kind, ids in (("refunds", refunds), ("writeoffs", writeoffs)):
        item = "refund" if kind == "refunds" else "writeoff"
        r = admin.post(f"{API}/org/{kind}/{ids[0]}/void", json={"reason": "Ошибка ввода"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "BACKDATE_OWNER_ONLY", (kind, r.text)               # 8 days: refused
        assert admin.post(f"{API}/org/{kind}/{ids[0]}/void", params={"dry_run": "true"}, json={"reason": "Ошибка ввода"}).status_code == 403
        assert admin.post(f"{API}/org/{kind}/{ids[1]}/void", json={"reason": "Ошибка ввода"}).status_code == 200, item      # 7 days: allowed
        assert owner(world).post(f"{API}/org/{kind}/{ids[0]}/void", json={"reason": "Ошибка ввода"}).status_code == 200      # the owner
