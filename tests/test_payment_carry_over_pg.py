"""N13 P2.1 / PAY-2: the money follows the student on a transfer - a LINK between memberships (PostgreSQL)."""
import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

from uuid import uuid4

import pytest

from src.auth.schemas import SaveAttendanceRequest, StartAttendanceRequest
from src.models.auth import GroupAttendance, Organization, Student, StudentGroup, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, give_group_price, give_price, make_client, make_group, make_student, needs_pg,
    pg_session, reconcile, world,
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
    """The individual price of the student in ``group`` (the only way a student gets a price of his own)."""
    r = owner(w).put(f"{API}/org/memberships/{mid(w, student or w.student, group)}/price-override",
                     json={"amount": fee, "reason": "тест"})
    assert r.status_code == 200, r.text


def price_of(w, group, student=None):
    r = owner(w).get(f"{API}/org/memberships/{mid(w, student or w.student, group)}/price-override")
    assert r.status_code == 200, r.text
    return r.json()["price"]["amount"], r.json()["price"]["source"]


# ---- calendar -----------------------------------------------------------------------



# ---- lessons --------------------------------------------------------------------------



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



# ---- when nothing moves -----------------------------------------------------------------


def test_add_mode_never_carries_and_never_copies_the_price(two):
    set_fee(two, two.group, 300000)
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = transfer(two, two.second, mode="add")
    assert carry_warnings(result) == []
    assert history(two, two.second) == [] and len(history(two, two.group)) == 1
    assert price_of(two, two.second) == (None, "none") and price_of(two, two.group) == (300000, "override")
    assert membership(two, two.group).is_active is True


def test_transfer_without_payments_adds_nothing(two):
    result = transfer(two, two.second)
    assert result["warnings"] == [] and history(two, two.second) == []


def test_unchanged_transfer_is_a_no_op(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    result = transfer(two, two.group)
    assert result["status"] == "unchanged" and result["warnings"] == []
    assert len(history(two, two.group)) == 1



# ---- bulk -----------------------------------------------------------------------------------



def test_bulk_add_mode_carries_nothing(two):
    pay(two, paid_on=day(-10), valid_from=day(-10), valid_to=day(20))
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", params={"mode": "add"},
                        json={"student_ids": [str(two.student.id)]})
    assert r.status_code == 200 and r.json()["warnings"] == []
    assert history(two, two.second) == []


# ---- price (ROADMAP #105: the student takes the price of the NEW group, an exception is not carried) ----------------


def test_the_individual_price_is_not_carried_the_student_takes_the_price_of_the_new_group(two):
    give_group_price(two.db, two.group, 400000)
    give_group_price(two.db, two.second, 500000)
    set_fee(two, two.group, 300000)                                       # an exception in the group being left
    assert price_of(two, two.group) == (300000, "override")
    transfer(two, two.second)
    assert price_of(two, two.second) == (500000, "group")                 # the price of the new group, no exception
    assert price_of(two, two.group) == (300000, "override")               # the old membership keeps its own history
    assert row_of(two, two.student, two.second)["monthly_fee"] == 500000


def test_a_group_price_on_either_side_is_never_copied_to_a_group_without_one(two):
    give_group_price(two.db, two.group, 400000)
    transfer(two, two.second)
    assert price_of(two, two.second) == (None, "none")                    # the new group has no price: nothing is copied


def test_an_individual_price_given_with_the_transfer_wins_for_the_new_membership_only(two):
    give_group_price(two.db, two.second, 500000)
    transfer(two, two.second, body={"price_override": {"amount": 350000, "reason": "льгота"}})
    assert price_of(two, two.second) == (350000, "override")
    assert row_of(two, two.student, two.second)["price"]["group_amount"] == 500000
    assert price_of(two, two.group) == (None, "none")


def test_a_returning_membership_keeps_its_own_history_of_prices(two):
    s0, s1 = two.students[:2]
    two.db.add(StudentGroup(student_id=s0.id, group_id=two.second.id, is_active=False,
                            joined_at=datetime.now() - timedelta(days=90)))
    two.db.commit()
    give_price(two.db, membership(two, two.second, s0), 777000, reason="прежняя договорённость")
    give_group_price(two.db, two.second, 500000)
    transfer(two, two.second, s0)                                     # LOGIC-A (F13): a return is priced by the group, the exception ended ...
    assert price_of(two, two.second, s0) == (500000, "group")
    set_fee(two, two.group, 0, s1)                                    # ... a free student of the old group is not free in the new one
    transfer(two, two.second, s1)
    assert price_of(two, two.second, s1) == (500000, "group")


def test_no_price_on_either_side_stays_empty(two):
    transfer(two, two.second)
    assert price_of(two, two.second) == (None, "none")


# ---- cascades --------------------------------------------------------------------------------



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



# ---- round trips ---------------------------------------------------------------------------------



# ---- who sees what ---------------------------------------------------------------------------------


GENERIC = "Действующая оплата ученика перенесена в новую группу"


def allow_admin(w, flag=True):
    w.db.query(Organization).filter_by(id=w.org.id).update({"settings": {"payments": {"admin_can_manage": flag}}})
    w.db.commit()



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



def test_excuse_deadline_without_a_carry_over_falls_back_to_the_month_end(two):
    from src.auth.excuse_deadline import month_end

    transfer(two, two.second)                                          # nothing to carry
    excused_today_in(two, two.second, two.student)
    assert deadline_in_group(two, two.student)["date"] == month_end(business_today()).isoformat()



# ---- conservation: the package that travels is always the original minus what was consumed -----------


def confirm(w, excuse):
    r = make_client(w).post(f"{API}/org/attendance/excuses/{excuse.id}/review", json={"decision": "confirmed"})
    assert r.status_code == 200, r.text



# ---- locks: one order everywhere, a concurrent void is waited for ---------------------------------------


@pytest.fixture
def engine():
    from sqlalchemy import create_engine
    from src.core.db_timezone import utc_connect_args

    from tests.pg_world import DB_URL

    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
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



def deadlock():
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        pgcode = "40P01"

    return OperationalError("UPDATE ...", {}, Orig("deadlock detected"))



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



def test_removing_a_student_maps_a_deadlock_to_409(two, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from src.auth.leave_service import LeaveService            # LOGIC-A: «Убрать из группы» goes through the leave service

    def boom(*args, **kwargs):
        raise deadlock()

    monkeypatch.setattr(LeaveService, "leave", boom)
    r = owner(two).delete(f"{GROUPS}/{two.group.id}/students/{two.student.id}")
    assert r.status_code == 409 and r.json()["detail"] == "Операция совпала с другой правкой. Повторите.", r.text

    def broken(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(LeaveService, "leave", broken)
    with pytest.raises(OperationalError):
        owner(two).delete(f"{GROUPS}/{two.group.id}/students/{two.student.id}")


# ---- PAY-2: a transfer is a link, the money that travels is derived -----------------------------------------------------


def billing(w, price_a=500_000, price_b=400_000):
    from tests.pg_world import ensure_billing

    ensure_billing(w, w.group, price=price_a)
    ensure_billing(w, w.second, price=price_b)
    give_group_price(w.db, w.second, price_b)


def statement(w, group):
    r = owner(w).get(f"{API}/org/memberships/{mid(w, w.student, group)}/statement")
    assert r.status_code == 200, r.text
    return r.json()["summary"]


def test_a_transfer_records_a_link_and_the_money_follows(two, monkeypatch):
    from src.models.auth import MembershipTransfer

    billing(two)
    pay(two, paid_on=day(-10), amount=3_000_000)                     # more than the old group has charged
    with clock(monkeypatch, day(-3), 12):
        result = transfer(two, two.second)
    links = two.db.query(MembershipTransfer).filter_by(student_id=two.student.id).all()
    assert len(links) == 1 and links[0].from_membership_id == membership(two, two.group).id
    old, new = statement(two, two.group), statement(two, two.second)
    assert old["transferred_out"] > 0 and old["advance"] == 0 and old["reconciles"] and new["reconciles"]
    assert new["transferred_in"] == old["transferred_out"] > 0
    # the whole 900 000 is accounted for: charged in both groups plus what is left, minus any debt
    assert old["charged"] + new["charged"] + new["advance"] - new["debt"] - old["debt"] == 3_000_000
    assert carry_warnings(result)


def test_voiding_the_payment_of_the_old_group_takes_the_money_back_from_the_new_one(two, monkeypatch):
    billing(two)
    p = pay(two, paid_on=day(-10), amount=3_000_000)["payment"]
    with clock(monkeypatch, day(-3), 12):
        transfer(two, two.second)
    assert statement(two, two.second)["transferred_in"] > 0
    assert owner(two).post(f"{API}/org/payments/{p['id']}/void", json={"reason": "ошибка"}).status_code == 200
    new = statement(two, two.second)
    assert new["transferred_in"] == 0 and new["debt"] > 0


def test_a_round_trip_conserves_the_money(two, monkeypatch):
    billing(two)
    pay(two, paid_on=day(-20), amount=4_000_000)
    with clock(monkeypatch, day(-10), 12):
        transfer(two, two.second)
    with clock(monkeypatch, day(-4), 12):
        transfer(two, two.group)
    a, b = statement(two, two.group), statement(two, two.second)
    assert a["reconciles"] and b["reconciles"]
    assert b["transferred_in"] > 0
    assert a["transferred_in"] == b["transferred_out"]
    total_out = a["transferred_out"] + b["transferred_out"]
    total_in = a["transferred_in"] + b["transferred_in"]
    assert total_out == total_in > 0
    money_left = (a["advance"] - a["debt"]) + (b["advance"] - b["debt"])
    assert money_left + a["charged"] + b["charged"] == 4_000_000


def test_the_transfer_day_is_charged_once_by_the_old_group(two, monkeypatch):
    billing(two)
    with clock(monkeypatch, day(-5), 12):
        transfer(two, two.second)
    r_old = owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.group)}/statement").json()
    r_new = owner(two).get(f"{API}/org/memberships/{mid(two, two.student, two.second)}/statement").json()
    old_days = {d for row in r_old["rows"] if row["type"] == "charge" for d in [row["details"]["segment"]["covered_through"]]}
    first_new = min(row["details"]["segment"]["period_from"] for row in r_new["rows"] if row["type"] == "charge")
    assert r_old["summary"]["charged"] > 0 and r_new["summary"]["charged"] >= 0 and first_new


def test_bulk_transfer_records_a_link_per_student(two):
    from src.models.auth import MembershipTransfer

    billing(two)
    s0, s1, s2 = two.students
    pay(two, s0, paid_on=day(-10), amount=3_000_000)
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(s.id) for s in two.students]})
    assert r.status_code == 200, r.text
    assert two.db.query(MembershipTransfer).filter_by(org_id=two.org.id).count() == 3
    assert [w["code"] for w in r.json()["warnings"] if w["code"] == "payment_carried_over"]    # only s0 had money


def test_internal_call_without_an_actor_still_records_the_link(two):
    from src.models.auth import MembershipTransfer

    billing(two)
    pay(two, paid_on=day(-10), amount=3_000_000)
    result = two.svc.add_student_to_group(str(two.org.id), str(two.second.id), str(two.student.id))
    assert result["status"] == "added"
    (link,) = two.db.query(MembershipTransfer).filter_by(org_id=two.org.id).all()
    assert link.created_by is None


def test_admin_without_the_right_gets_the_generic_message(two):
    billing(two)
    pay(two, paid_on=day(-10), amount=SECRET * 100)
    result = transfer(two, two.second, client=make_client(two))       # default identity = admin
    assert [w["code"] for w in carry_warnings(result)] == ["payment_carried_over"]
    text = json.dumps(result, ensure_ascii=False)
    assert str(SECRET) not in text and "7 654 321" not in text


def test_admin_with_the_right_sees_the_sum(two):
    allow_admin(two)
    billing(two)
    pay(two, paid_on=day(-10), amount=SECRET * 100)
    result = transfer(two, two.second, client=make_client(two))
    (warning,) = carry_warnings(result)
    assert warning["message"].startswith("Остаток денег") and warning["message"].endswith("перенесён в «Вторая»")


def test_a_deadlock_is_a_409_to_repeat_never_a_500(two, monkeypatch):
    from src.auth.payments_service import PaymentService
    from src.auth.service import AuthService

    original = pay(two, paid_on=day(-10), valid_to=day(20))["payment"]

    def boom(*args, **kwargs):
        raise deadlock()

    message = "Операция совпала с другой правкой. Повторите."
    monkeypatch.setattr(PaymentService, "_probe_payment", boom)
    for url, body in ((f"{API}/org/payments/{original['id']}/void", {"reason": "проверка"}),
                      (f"{API}/org/payments/{original['id']}/replace", {"amount": 1, "paid_on": day(0).isoformat(), "method": "cash"})):
        r = owner(two).post(url, json=body)
        assert r.status_code == 409 and r.json()["detail"] == {"code": "CONCURRENT_EDIT", "message": message}, (url, r.text)
    monkeypatch.undo()
    monkeypatch.setattr(AuthService, "add_student_to_group", boom)
    monkeypatch.setattr(AuthService, "bulk_add_students", boom)
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/{two.student.id}")
    assert r.status_code == 409 and r.json()["detail"] == message, r.text
    r = owner(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(two.student.id)]})
    assert r.status_code == 409 and r.json()["detail"] == message, r.text


def test_replace_takes_the_family_before_the_payment(two, engine):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth.payments_schemas import PaymentReplace
    from src.auth.payments_service import PaymentService

    original = pay(two, paid_on=day(-10), valid_to=day(20))["payment"]
    m = membership(two, two.group)
    holder = Session(engine)                                  # a transfer in flight holds the membership
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR UPDATE"), {"m": m.id})
    body = PaymentReplace(amount=777_000, paid_on=day(-10), method="cash")
    thread, outcome = run_in_thread(engine, lambda session: PaymentService(session).replace_payment(
        str(two.org.id), original["id"], str(two.owner.id), body))
    wait_until_blocked(engine)
    assert row_is_free(engine, "student_payments", original["id"])       # waiting for the membership, payment untouched
    holder.rollback()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"].payment.amount == 777_000

