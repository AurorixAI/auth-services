"""Money-1 pack B: shared helpers of the PostgreSQL tests (a group with a real schedule, the account of a membership through the real loader)."""
from datetime import date

from sqlalchemy.orm.attributes import flag_modified

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.auth import payment_status as ps


def slot(slot_id: str, weekday: int, valid_from: str, start: str = "10:00", end: str = "11:00") -> dict:
    """A weekly slot of the schedule of a group (``weekday``: 1 = Monday)."""
    return {"id": slot_id, "dayOfWeek": weekday, "startTime": start, "endTime": end, "format": "offline", "validFrom": valid_from}


def set_schedule(w, slots, group=None):
    group = group or w.group
    group.schedule = list(slots)
    flag_modified(group, "schedule")
    w.db.commit()


def account(w, membership_id, today: date | None = None) -> bl.Ledger:
    """The ledger of a membership by the REAL loader (the schedule, the prices, the payments of the database)."""
    w.db.expire_all()
    loader = ls.LedgerLoader(w.db, str(w.org.id), today or ps.business_today())
    loader.load_facts([membership_id])
    return bl.run_ledger(loader.build_input(membership_id))


def charge_of(ledger: bl.Ledger, period_start: date) -> int:
    """The charge of the period (cell) that starts on ``period_start``."""
    (seg,) = [s for s in ledger.segments if s.kind == "period" and s.period_start == period_start]
    return seg.amount


def hold_lessons(w, student, days, group=None):
    """Held (completed) lessons of the group on ``days`` where ``student`` was present (the rows the package counts; the lesson id is the one of the weekday slot of the test group)."""
    from src.models.auth import GroupAttendance

    group = group or w.group
    for day in days:
        w.db.add(GroupAttendance(org_id=w.org.id, group_id=group.id, lesson_date=day.isoformat(), lesson_id=f"s{day.isoweekday()}", status="completed",
                                 records={str(student.id): "present"}, roster_snapshot=[{"student_id": str(student.id)}]))
    w.db.commit()


# ---- the world of «Оплата с» and of the transfers (shared by several files) ----------------------------------------------------------------------------------

OPENS = [slot("mon", 1, "2026-11-05"), slot("wed", 3, "2026-11-05"), slot("fri", 5, "2026-11-05")]      # the group opens on Thursday 05.11: the first lesson is Friday 06.11


def opening_world(w, fixed_today, today: date = date(2026, 10, 21), schedule=None):
    from tests.pg_world import give_group_price
    from tests.test_pay2_api_pg import settings_block

    fixed_today(today)
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000)
    set_schedule(w, OPENS if schedule is None else schedule)


def fresh_student(w, name="Новенький"):
    """A student of the centre who is in no group."""
    from src.models.auth import Student

    student = Student(org_id=w.org.id, first_name=name, last_name="Ученик", class_level=9, status="new")
    w.db.add(student)
    w.db.commit()
    return student


def add(w, student, *, client=None, expect=200, mode="add", group=None, **params):
    """«Добавить в группу» through the route; ``pay_from`` and the like go into the body, ``dry_run`` into the query."""
    from tests.test_pay2_api_pg import API, owner

    body = {k: v for k, v in params.items() if k != "dry_run"}
    r = (client or owner(w)).post(f"{API}/org/groups/{(group or w.group).id}/students/{student.id}",
                                  params={"mode": mode, **({"dry_run": "true"} if params.get("dry_run") else {})}, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def opening_until(w, m, until):
    """An opening balance «оплачено до» (no money): the notebook of the centre."""
    from src.models.auth import MembershipOpeningBalance

    w.db.add(MembershipOpeningBalance(org_id=w.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, paid_until=until, source="manual", created_by=w.owner.id))
    w.db.commit()


def transfer(w, student, to, *, dry_run=False):
    from tests.test_pay2_api_pg import API, owner

    r = owner(w).post(f"{API}/org/groups/{to.id}/students/{student.id}", params={"mode": "transfer", **({"dry_run": "true"} if dry_run else {})}, json={})
    assert r.status_code == 200, r.text
    return r.json()
