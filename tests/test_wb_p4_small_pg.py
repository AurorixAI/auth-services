"""Wave B P4: the small items of B1 and B2, each with the scenario that shows it.

MN-50 (a carry-over copy cannot be voided), MN-06 (a package read with nothing to read from is empty, not a crash), MN-25 (the accounting start is a Tashkent
day), MN-35 (a newer rate always wins), DM-006 (leaving locks the student without blocking rows that point at him), SV-17 / SV-18 (the start of a lesson),
DM-003 / DM-004 / DM-005 / MN-03 / MN-22 (the clock: ``bc.set_clock`` reaches every stamp, the excuse times are UTC).
The clock is ``bc.set_clock`` (never ``monkeypatch(datetime)``).
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from src.auth import business_clock as bc
from src.auth import payment_status as ps
from src.auth.payments_service import PaymentService
from src.models.auth import StudentPayment

from tests.pg_world import excused_lesson, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_attendance_excuses import excuse_of
from tests.test_pay2_api_pg import API, membership, owner, seed_payment, settings_block, stay
from tests.test_pay2_loader_pg import join_on, loader, membership_of, set_payments_settings

pytestmark = needs_pg
D = date


@pytest.fixture
def frozen():
    """The business clock stands at a given Tashkent wall time; nothing else is patched."""
    def set_to(local: datetime):
        bc.set_clock(lambda: local.replace(tzinfo=bc.BUSINESS_TIMEZONE))
    yield set_to
    bc.set_clock(None)


# ---- MN-50 --------------------------------------------------------------------------------------------------------------------------------


def test_mn50_a_carry_over_copy_cannot_be_voided_but_the_payment_it_came_from_can(world):
    settings_block(world, accounting_start_date="2026-09-01")
    m = membership(world)
    stay(world, m, D(2026, 9, 15))
    real = seed_payment(world, m, 100_000, D(2026, 9, 20), receipt_no=1)
    carry = seed_payment(world, m, 0, D(2026, 9, 20), receipt_no=2)
    carry.kind = "legacy_carry"
    carry.carried_from_payment_id = real.id
    world.db.commit()
    c = owner(world)
    r = c.post(f"{API}/org/payments/{carry.id}/void", json={"reason": "по ошибке"})
    assert r.status_code == 409 and "Перенос оплаты" in str(r.json()["detail"]), r.text
    world.db.expire_all()
    assert world.db.get(StudentPayment, carry.id).voided_at is None, "the refusal writes nothing"
    ok = c.post(f"{API}/org/payments/{real.id}/void", json={"reason": "по ошибке"})
    assert ok.status_code == 200, ok.text


# ---- MN-06 --------------------------------------------------------------------------------------------------------------------------------


def test_mn06_a_package_read_with_nothing_to_read_from_is_empty_for_every_membership(world):
    m = membership(world)
    out = PaymentService(world.db)._counted_lesson_dates(str(world.org.id), [(m, world.students[0], world.group)], since=None)
    assert out == {m.id: []}


# ---- MN-25 --------------------------------------------------------------------------------------------------------------------------------


def test_mn25_a_student_who_left_at_night_of_the_accounting_start_day_is_in_the_list_of_all(world):
    """The accounting starts on 10.09 (a Tashkent day = from 09.09 19:00 UTC).  He left at 02:00 Tashkent of 10.09 (= 09.09 21:00 UTC), no money record:
    he stayed ON the first day, so he is counted (before the fix the boundary was 10.09 00:00 UTC and he dropped out)."""
    db = world.db
    set_payments_settings(db, world.org, accounting_start_date="2026-09-10")
    left = membership_of(db, world.students[1], world.group)
    join_on(db, left, D(2026, 8, 1), left=D(2026, 8, 20))
    left.left_at = datetime(2026, 9, 9, 21, 0)
    (period,) = left.periods
    period.left_at = left.left_at
    left.is_active = False
    db.commit()
    assert left.id in set(loader(world, D(2026, 10, 21)).select(scope="all"))
    just_before = datetime(2026, 9, 9, 18, 59)          # 23:59 Tashkent of 09.09: before the accounting start
    left.left_at = just_before
    period.left_at = just_before
    db.commit()
    assert left.id not in set(loader(world, D(2026, 10, 21)).select(scope="all"))


# ---- MN-35 --------------------------------------------------------------------------------------------------------------------------------


def test_mn35_a_rate_written_with_a_clock_that_stepped_back_still_wins(world, frozen):
    frozen(datetime(2026, 11, 2, 12, 0))
    c = owner(world)
    url = f"{API}/org/payroll/rates/{world.teacher.id}"
    first = c.put(url, json={"valid_from": "2026-11", "percents": {"group": "50"}})
    assert first.status_code == 200, first.text
    frozen(datetime(2026, 11, 2, 11, 0))                  # the clock went an hour BACK (another instance, an NTP step)
    second = c.put(url, json={"valid_from": "2026-11", "percents": {"group": "60"}})
    assert second.status_code == 200, second.text
    from src.auth import payroll_rates

    book = payroll_rates.load_book(world.db, str(world.org.id), [world.teacher.id])
    assert float(book.percents_at(world.teacher.id, D(2026, 11, 1))["group"]) == 60.0, "the later write wins even if its clock is older"


# ---- DM-006 / SV-17 -----------------------------------------------------------------------------------------------------------------------


def test_dm006_leaving_locks_the_student_with_the_weak_lock_so_a_row_that_points_at_him_does_not_wait(world, frozen):
    """While ``leave`` runs, a second connection inserts a row with an FK to the student (a payment's file / audit): with ``FOR UPDATE`` it waited for the commit
    (and could deadlock), with ``FOR NO KEY UPDATE`` it does not.  Checked on the statement that locks the student."""
    frozen(datetime(2026, 11, 2, 12, 0))
    statements = []

    @event.listens_for(world.db.get_bind(), "before_cursor_execute")
    def grab(conn, cursor, statement, parameters, context, executemany):
        if "FROM students" in statement and "FOR " in statement:
            statements.append(statement)

    from src.auth.leave_service import LeaveService

    LeaveService(world.db).leave(str(world.org.id), str(world.group.id), str(world.students[0].id), str(world.owner.id), "owner")
    event.remove(world.db.get_bind(), "before_cursor_execute", grab)
    assert statements, "the student row was locked"
    assert all("FOR NO KEY UPDATE" in s and "FOR UPDATE" not in s.replace("FOR NO KEY UPDATE", "") for s in statements), statements


def test_sv17_starting_a_lesson_locks_the_teacher_with_the_weak_lock():
    import inspect

    from src.auth import service

    source = inspect.getsource(service.AuthService.start_group_attendance)
    assert "User.id == lesson_teacher_id).with_for_update(key_share=True)" in source.replace("\r", "")


# ---- DM-003 / DM-004 / DM-005 / MN-03 / MN-22: the clock ---------------------------------------------------------------------------------


def test_mn22_the_business_today_of_the_payments_follows_the_business_clock(frozen):
    frozen(datetime(2026, 12, 31, 23, 30))                # 23:30 Tashkent = 18:30 UTC of the same day
    assert ps.business_today() == D(2026, 12, 31)
    frozen(datetime(2027, 1, 1, 0, 30))                   # 00:30 Tashkent = 19:30 UTC of 31.12: the business day is already the next one
    assert ps.business_today() == D(2027, 1, 1)
    assert ps.business_date(datetime(2026, 12, 31, 19, 30)) == D(2027, 1, 1)


@pytest.mark.parametrize("module,name", [
    ("payments_service", "_utcnow"), ("pricing", "_utcnow"), ("payroll_payouts", "_utcnow"), ("payroll_service", "_utcnow"),
    ("student_archive", "_utcnow_naive"), ("membership_periods", "utcnow_naive"),
])
def test_mn03_dm005_every_stamp_of_the_money_modules_reads_the_one_clock(module, name, frozen):
    import importlib

    frozen(datetime(2026, 12, 31, 23, 59, 30))            # Tashkent; the UTC instant is 18:59:30
    stamp = getattr(importlib.import_module(f"src.auth.{module}"), name)()
    assert stamp == datetime(2026, 12, 31, 18, 59, 30) and stamp.tzinfo is None


def test_as_utc_makes_every_stored_moment_comparable(frozen):
    assert bc.as_utc(None) is None
    assert bc.as_utc(datetime(2026, 1, 1, 10)) == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    aware = datetime(2026, 1, 1, 15, tzinfo=timezone(timedelta(hours=5)))
    assert bc.as_utc(aware) == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)


def test_dm003_the_excuse_times_are_utc_in_the_table_and_tashkent_wall_in_the_api(world, frozen):
    """One convention for ``attendance_excuses.*_at`` (naive UTC, like every other table).  The API keeps showing the Tashkent wall time (the front prints it as it is):
    15:00 Tashkent is stored as 10:00 and shown as 15:00 - and a decision a half hour later sorts AFTER the mark whichever of the two writers made it."""
    frozen(datetime(2026, 10, 21, 15, 0))
    excused_lesson(world)
    excuse = excuse_of(world)
    assert excuse.reason_at == datetime(2026, 10, 21, 10, 0)
    frozen(datetime(2026, 10, 21, 15, 30))
    r = make_client(world, user=world.owner).post(f"/api/v1/auth/org/attendance/excuses/{excuse.id}/review", json={"decision": "confirmed"})
    assert r.status_code == 200, r.text
    item = r.json()
    assert (item["reason_at"], item["reviewed_at"]) == ("2026-10-21T15:00:00", "2026-10-21T15:30:00")
    stored = excuse_of(world)
    assert stored.reviewed_at == datetime(2026, 10, 21, 10, 30) and stored.reviewed_at > stored.reason_at
